"""全量调用落库。这一层没有「可选」这个选项。

SPEC §4.1 给了三个理由，缺一不可：
1. LLM 被允许独立出结论，「为什么今天这么说」必须答得上来——审计能力是信任的前提；
2. 将来训本地 30B，llm_calls 就是现成的数据集；
3. 换模型时拿历史输入做回归对比，否则每次换模型都是盲飞。

所以入口只有 `invoke()` 一个：先调用、后落库、落库不成功就不算调用完成。
业务代码不允许直接拿 provider 调——那条路绕过审计，等于三个理由全废。
"""

from dataclasses import replace
from datetime import datetime, timezone
from time import perf_counter

from sqlalchemy.orm import Session

from tin.llm.base import (
    STATUS_ERROR, LlmError, LlmProvider, LlmRequest, LlmResponse, usage_of,
)
from tin.models import LlmCall


class AuditError(LlmError):
    """落库本身失败。调用可能已经发出去并花了钱，但没有留痕，一律当失败处理。"""


def invoke(session: Session, provider: LlmProvider, request: LlmRequest) -> LlmResponse:
    """调用 LLM 并把这一次完整写进 `llm_calls`，然后才返回。

    三条路径都落库，一条不漏：
    - 正常返回 → status=ok
    - 模型拒答 → status=refusal（有响应体，不是故障）
    - 抛异常 / 响应不可用 → status=error，随后抛 LlmError

    `session` 用调用方的：简报组装是一个事务，调用记录和简报要么一起成立要么一起不成立。
    """
    started = perf_counter()
    try:
        response = provider.complete(request)
    except LlmError:
        raise
    except Exception as exc:  # noqa: BLE001 — provider 抛什么都要留痕
        response = LlmResponse(
            status=STATUS_ERROR, provider=getattr(provider, "name", "?"),
            model=getattr(provider, "model", ""), error=f"{type(exc).__name__}: {exc}",
        )
        response = _persist(session, provider, request, response, _elapsed_ms(started))
        raise LlmError(f"{provider.name} 调用失败：{response.error}") from exc

    response = _persist(session, provider, request, response, _elapsed_ms(started))
    if response.status == STATUS_ERROR:
        # 响应体已经进库了，这里只是不让上层拿到一个用不了的结果继续往下走
        raise LlmError(f"{provider.name} 返回了不可用的响应：{response.error}")
    return response


def _elapsed_ms(started: float) -> int:
    return int((perf_counter() - started) * 1000)


def _persist(session: Session, provider: LlmProvider, request: LlmRequest,
             response: LlmResponse, elapsed_ms: int) -> LlmResponse:
    """写 llm_calls 并 commit。写不进去就抛 AuditError——没有留痕的调用不算数。"""
    if response.latency_ms is None:
        response = replace(response, latency_ms=elapsed_ms)
    row = LlmCall(
        purpose=request.purpose,
        provider=response.provider or provider.name,
        model=response.model or provider.model,
        request=request.dump(),
        response=response.dump(),
        latency_ms=response.latency_ms,
        status=response.status,
        error=response.error,
        brief_id=request.brief_id,
        created_at=datetime.now(timezone.utc),
        **usage_of(response),
    )
    session.add(row)
    try:
        session.commit()
    except Exception as exc:  # noqa: BLE001
        session.rollback()
        raise AuditError(f"LLM 调用落库失败，本次调用不算完成：{exc}") from exc
    return replace(response, call_id=row.id)
