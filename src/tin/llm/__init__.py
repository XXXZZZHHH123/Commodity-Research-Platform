"""LLM 接入层（SPEC §4.1 / F2）。三件事，按重要性排：

1. **provider 抽象** —— 业务代码只依赖 `LlmProvider`；从 Anthropic 切到内网 30B
   只换 `TIN_LLM_PROVIDER`，不改一行业务代码；
2. **全量调用落库** —— 走 `audit.invoke()`，一条不漏；
3. **证据强制校验闸门** —— 走 `guard.compose()`，引用对不上就打回。

业务代码的正确用法只有一条：

    provider = build_provider()
    result = guard.compose(session, provider, request, ctx)

不要直接调 `provider.complete()`——那条路绕过审计，SPEC §4.1 的三个理由全废。
"""

from tin.config import settings
from tin.llm.base import (
    MAX_CACHE_BREAKPOINTS, STATUS_ERROR, STATUS_OK, STATUS_REFUSAL, STATUSES,
    LlmError, LlmProvider, LlmRefused, LlmRequest, LlmResponse, Message,
    ProviderUnavailable, SystemBlock,
)
from tin.llm.guard import EvidenceContext, GuardResult

__all__ = [
    "EvidenceContext", "GuardResult", "LlmError", "LlmProvider", "LlmRefused",
    "LlmRequest", "LlmResponse", "MAX_CACHE_BREAKPOINTS", "Message",
    "ProviderUnavailable", "STATUSES", "STATUS_ERROR", "STATUS_OK", "STATUS_REFUSAL",
    "SystemBlock", "build_provider",
]


def build_provider(name: str | None = None, **kwargs) -> LlmProvider:
    """按配置造一个 provider。**这是业务代码唯一该知道的 provider 构造入口。**

    延迟导入各实现：`openai` 不在 requirements.txt 里，没装它也不能妨碍
    其余代码 import `tin.llm`。
    """
    name = (name or settings.llm_provider).strip().lower()

    if name in ("anthropic", "claude"):
        from tin.llm.anthropic_provider import AnthropicProvider
        return AnthropicProvider(**kwargs)
    if name in ("openai_compatible", "openai", "vllm", "ollama", "local"):
        from tin.llm.openai_provider import OpenAICompatibleProvider
        return OpenAICompatibleProvider(**kwargs)
    if name == "fake":
        from tin.llm.fake import FakeProvider
        return FakeProvider(kwargs.pop("script", []), **kwargs)

    raise ProviderUnavailable(
        f"未知的 LLM provider：{name!r}；可选 anthropic / openai_compatible / fake")
