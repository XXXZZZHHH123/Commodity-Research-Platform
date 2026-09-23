"""Anthropic API 实现（现阶段的默认 provider）。

几条容易踩的，写在前面：
- **认证不由我们管**。零参数构造 `Anthropic()`，SDK 自己按
  `ANTHROPIC_API_KEY` → `ANTHROPIC_AUTH_TOKEN` → `ant auth login` 存的 OAuth profile
  逐个解析。所以「环境变量没设」不等于「没有凭据」，这里不做任何 key 存在性检查，
  更不硬编码 key。
- **不传 temperature / top_p / top_k**。新模型上传了直接 400。
  要控风格就改 prompt，要控深浅就调 effort。
- **输出走 json_schema 强约束**，绝不解析自由文本。
- **缓存断点打在 system 块上**。system 在 messages 之前渲染，所以断点之后追加对话
  不会让前缀作废——打回重试就是靠这条省钱的。
"""

import json
from time import perf_counter

from tin.config import settings
from tin.llm.base import (
    STATUS_ERROR, STATUS_OK, STATUS_REFUSAL, LlmRequest, LlmResponse,
    ProviderUnavailable, dump_object,
)

# 服务端拒答时换模型重试的 beta 开关。切到内网自托管端点后它没有意义，用 settings 关掉。
FALLBACK_BETA = "server-side-fallback-2026-07-01"


class AnthropicProvider:
    """SPEC §4.1 的两个 provider 实现之一。"""

    name = "anthropic"

    def __init__(self, *, model: str | None = None, timeout: float | None = None,
                 max_retries: int | None = None, fallback: bool | None = None,
                 client=None):
        self.model = model or settings.llm_model
        self._fallback = settings.llm_fallback if fallback is None else fallback
        if client is not None:
            self._client = client
            return
        try:
            from anthropic import Anthropic
        except ImportError as exc:  # pragma: no cover - 依赖已在 requirements.txt
            raise ProviderUnavailable(
                "未安装 anthropic SDK，执行 pip install anthropic") from exc
        # 零参数解析凭据：这里只传超时与重试，绝不传 api_key
        self._client = Anthropic(
            timeout=settings.llm_timeout if timeout is None else timeout,
            max_retries=settings.llm_max_retries if max_retries is None else max_retries,
        )

    # ------------------------------------------------------------------ 调用

    def complete(self, request: LlmRequest) -> LlmResponse:
        kwargs = {
            "model": self.model,
            "max_tokens": request.max_tokens,
            "messages": [m.dump() for m in request.messages],
        }
        if request.system:
            kwargs["system"] = self._system_blocks(request)
        if request.output_schema is not None:
            kwargs["output_config"] = {
                "format": {"type": "json_schema", "schema": request.output_schema}
            }

        started = perf_counter()
        if self._fallback:
            # 安全分类器误伤时由服务端换模型续上，而不是把失败甩给研究员
            message = self._client.beta.messages.create(
                betas=[FALLBACK_BETA], fallbacks="default", **kwargs)
        else:
            message = self._client.messages.create(**kwargs)
        latency_ms = int((perf_counter() - started) * 1000)

        return self._to_response(message, request, latency_ms)

    # -------------------------------------------------------------- 请求翻译

    @staticmethod
    def _system_blocks(request: LlmRequest) -> list[dict]:
        blocks = []
        for block in request.system:
            item: dict = {"type": "text", "text": block.text}
            if block.cache_breakpoint:
                item["cache_control"] = {"type": "ephemeral"}
            blocks.append(item)
        return blocks

    # -------------------------------------------------------------- 响应翻译

    def _to_response(self, message, request: LlmRequest, latency_ms: int) -> LlmResponse:
        usage = getattr(message, "usage", None)
        common = {
            "model": getattr(message, "model", self.model) or self.model,
            "provider": self.name,
            "stop_reason": getattr(message, "stop_reason", None),
            "input_tokens": getattr(usage, "input_tokens", None),
            "output_tokens": getattr(usage, "output_tokens", None),
            "cache_read_tokens": getattr(usage, "cache_read_input_tokens", None),
            "cache_write_tokens": getattr(usage, "cache_creation_input_tokens", None),
            "latency_ms": latency_ms,
            "raw": dump_object(message),
        }

        # 拒答要在读 content 之前判：拒答时 content 可能是空的，直接下标会炸
        if common["stop_reason"] == "refusal":
            details = getattr(message, "stop_details", None)
            return LlmResponse(
                status=STATUS_REFUSAL,
                error=f"模型拒答，类别：{getattr(details, 'category', None)}",
                **common,
            )

        text = self._first_text(message)
        if request.output_schema is None:
            return LlmResponse(status=STATUS_OK, text=text, **common)

        if not text:
            return LlmResponse(status=STATUS_ERROR, text=text,
                               error="结构化输出为空（多半是 max_tokens 被思考占满了）", **common)
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            # 响应体照常进库——排查「模型到底吐了什么」全靠这一行
            return LlmResponse(status=STATUS_ERROR, text=text,
                               error=f"结构化输出不是合法 JSON：{exc}", **common)
        if not isinstance(data, dict):
            return LlmResponse(status=STATUS_ERROR, text=text,
                               error=f"结构化输出顶层不是对象，而是 {type(data).__name__}", **common)
        return LlmResponse(status=STATUS_OK, text=text, data=data, **common)

    @staticmethod
    def _first_text(message) -> str | None:
        for block in getattr(message, "content", None) or []:
            if getattr(block, "type", None) == "text":
                return getattr(block, "text", None)
        return None
