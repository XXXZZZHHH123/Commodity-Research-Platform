"""OpenAI 兼容端点实现。将来内网跑本地 30B（vLLM / Ollama）走这条。

设计要点：
- `openai` 包是**延迟导入**的。它不在 requirements.txt 里（现阶段没人用这条路），
  没装也绝不能阻塞 `tin.llm` 的其余部分被 import——否则整个 agent 层起不来。
- 没有 `cache_control` 这种东西。vLLM 的 prefix caching 是按**前缀自动**命中的，
  所以「把稳定内容放前面」这条纪律在这条路上同样成立，只是不用显式打断点。
  断点信息在这里被忽略，但 system 块的**顺序**照样保留。
- token 计数字段名不一样：prompt/completion_tokens，缓存命中在
  `prompt_tokens_details.cached_tokens`；没有「缓存写入」这个概念，留 None。
"""

import json
from time import perf_counter

from tin.config import settings
from tin.llm.base import (
    STATUS_ERROR, STATUS_OK, STATUS_REFUSAL, LlmRequest, LlmResponse,
    ProviderUnavailable, dump_object,
)


def _unfence(text: str) -> str:
    """剥掉 ```json ... ``` 围栏。

    json_schema 模式下服务端保证纯 JSON，用不上；但 json_object / none 模式下
    千问一类的模型习惯性套 markdown 围栏，不剥就是 JSONDecodeError——
    把一次本来成功的调用判成失败，白白多烧一轮重试。
    """
    s = text.strip()
    if not s.startswith("```"):
        return s
    s = s[3:]
    if s[:4].lower() == "json":
        s = s[4:]
    return s.rsplit("```", 1)[0].strip() if "```" in s else s.strip()


class OpenAICompatibleProvider:
    """SPEC §4.1 的两个 provider 实现之二。协议是 OpenAI 的，跑的是谁不关心。"""

    name = "openai_compatible"

    def __init__(self, *, model: str | None = None, base_url: str | None = None,
                 api_key: str | None = None, timeout: float | None = None,
                 max_retries: int | None = None, json_mode: str | None = None, client=None):
        self.model = model or settings.llm_model
        self.base_url = base_url or settings.llm_base_url
        self._mode = (json_mode or settings.llm_json_mode or "json_schema").strip()
        if self._mode not in ("json_schema", "json_object", "none"):
            raise ProviderUnavailable(
                f"TIN_LLM_JSON_MODE 只能是 json_schema / json_object / none，收到 {self._mode!r}")
        if client is not None:
            self._client = client
            return
        try:
            from openai import OpenAI
        except ImportError as exc:
            raise ProviderUnavailable(
                "未安装 openai 包，无法使用 OpenAI 兼容端点；"
                "执行 pip install openai（内网部署时放进离线包）"
            ) from exc
        if not self.base_url:
            raise ProviderUnavailable(
                "OpenAI 兼容端点缺少 base_url，设置 TIN_LLM_BASE_URL，"
                "例如 http://10.0.0.5:8000/v1")
        self._client = OpenAI(
            base_url=self.base_url,
            # 本地端点通常不校验密钥，但 SDK 要求非空；不硬编码任何真实密钥
            api_key=api_key or settings.llm_api_key or "not-needed",
            timeout=settings.llm_timeout if timeout is None else timeout,
            max_retries=settings.llm_max_retries if max_retries is None else max_retries,
        )

    # ------------------------------------------------------------------ 调用

    def complete(self, request: LlmRequest) -> LlmResponse:
        system_text = "\n\n".join(b.text for b in request.system) if request.system else ""
        kwargs: dict = {"model": self.model}

        if request.output_schema is None:
            # 自由文本才设 max_tokens
            kwargs["max_tokens"] = request.max_tokens
        else:
            # 结构化输出**不能**设 max_tokens：截断会产出半截 JSON，下游解析必炸。
            # 阿里云百炼的文档把这条写成了硬性要求，vLLM 上同样是这个道理。
            # 输出长度改由 prompt 里的篇幅要求控制，不靠硬截。
            rf = self._response_format(request)
            if rf is not None:
                kwargs["response_format"] = rf
            if self._mode != "json_schema":
                # 只有 json_schema 由服务端强约束形状。另外两种模式端点最多保证
                # 「是合法 JSON」，形状得贴 schema 给模型看，最终由证据闸门兜底。
                # 顺带满足百炼的硬要求：system/user 里必须出现「JSON」关键词，否则直接报错。
                system_text = "\n\n".join(filter(None, [system_text, self._schema_hint(request)]))

        messages: list[dict] = []
        if system_text:
            # 顺序即前缀：稳定的在前，易变的在后，与 Anthropic 侧保持同一条纪律
            messages.append({"role": "system", "content": system_text})
        messages.extend(m.dump() for m in request.messages)
        kwargs["messages"] = messages

        started = perf_counter()
        completion = self._client.chat.completions.create(**kwargs)
        latency_ms = int((perf_counter() - started) * 1000)
        return self._to_response(completion, request, latency_ms)

    # ---------------------------------------------------------- 结构化输出模式

    def _response_format(self, request: LlmRequest) -> dict | None:
        if self._mode == "json_schema":
            return {"type": "json_schema",
                    "json_schema": {"name": request.schema_name,
                                    "schema": request.output_schema, "strict": True}}
        if self._mode == "json_object":
            return {"type": "json_object"}
        return None  # 端点什么都不支持时，形状完全靠 _schema_hint + 闸门

    def _schema_hint(self, request: LlmRequest) -> str:
        """把 schema 贴给模型看。

        json_schema 模式下服务端会强约束，用不上这段；json_object / none 模式下
        端点只保证「是合法 JSON」，形状得靠说明，错了由证据闸门打回重试。
        文中出现「JSON」是硬要求——百炼没有这个词会直接报错。
        """
        return ("你必须只输出一个 JSON 对象，不要包含解释文字或 markdown 代码块标记。"
                "该 JSON 必须符合以下 JSON Schema：\n"
                + json.dumps(request.output_schema, ensure_ascii=False, indent=2))

    # -------------------------------------------------------------- 响应翻译

    def _to_response(self, completion, request: LlmRequest, latency_ms: int) -> LlmResponse:
        usage = getattr(completion, "usage", None)
        details = getattr(usage, "prompt_tokens_details", None)
        choice = (getattr(completion, "choices", None) or [None])[0]
        message = getattr(choice, "message", None)
        common = {
            "model": getattr(completion, "model", self.model) or self.model,
            "provider": self.name,
            "stop_reason": getattr(choice, "finish_reason", None),
            "input_tokens": getattr(usage, "prompt_tokens", None),
            "output_tokens": getattr(usage, "completion_tokens", None),
            "cache_read_tokens": getattr(details, "cached_tokens", None),
            "cache_write_tokens": None,  # 协议里没有这个概念，不要拿 0 冒充
            "latency_ms": latency_ms,
            "raw": dump_object(completion),
        }

        refusal = getattr(message, "refusal", None)
        if refusal:
            return LlmResponse(status=STATUS_REFUSAL, error=f"模型拒答：{refusal}", **common)

        text = getattr(message, "content", None)
        if request.output_schema is None:
            return LlmResponse(status=STATUS_OK, text=text, **common)

        if not text:
            return LlmResponse(status=STATUS_ERROR, text=text,
                               error="结构化输出为空", **common)
        try:
            data = json.loads(_unfence(text))
        except json.JSONDecodeError as exc:
            return LlmResponse(status=STATUS_ERROR, text=text,
                               error=f"结构化输出不是合法 JSON：{exc}", **common)
        if not isinstance(data, dict):
            return LlmResponse(status=STATUS_ERROR, text=text,
                               error=f"结构化输出顶层不是对象，而是 {type(data).__name__}", **common)
        return LlmResponse(status=STATUS_OK, text=text, data=data, **common)
