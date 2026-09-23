"""每日简报组装（SPEC F3 / §4.3 流水线里 `compose_brief` 那一步）。

一条直线，不绕：

    取生效判断(L1) → 读当日 signals → 按 scope 筛 playbook(L2)
      → prompt.build → llm.guard.compose（调用 + 证据闸门）→ brief.store.save

每一段都用上游已经写好的东西，这里只负责串。**三条不能破的分工**：

- 阈值有没有触发是 `compute/signals.py` 算出来的，这里只读 `signals` 表，一个字都不重判；
- 落库只能走 `store.save`，绝不自己拼 `DailyBrief`——闸门只有变成唯一通路才算数；
- 喂给 `EvidenceContext` 的集合必须与实际进了 prompt 的完全一致，多喂一个就开了个洞。

冷启动（playbook 为空）必须能跑通：那是每个新研究员的第一天，不是边界情况（§2.1）。
只要 L1 骨架还在，简报就出得来——第一印象决定他会不会用第二次。
"""

from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from tin.brief import prompt, store
from tin.judgments.service import current as current_judgment
from tin.llm import build_provider
from tin.llm.base import LlmProvider, LlmRequest
from tin.llm.guard import EvidenceContext
from tin.llm.guard import compose as guard_compose
from tin.models import DailyBrief, Signal


class ComposeError(Exception):
    """组装前置条件不成立。调用方（流水线 / 页面）应原样呈现原因。"""


def signals_of(session: Session, judgment_id: int, d: date) -> list[Signal]:
    """当日已落库的信号，顺序固定为 (rule_type, rule_id)。

    只读不算：`evaluate_signals` 在流水线里排在前面，这里再算一遍必然出现
    「页面说未触发、简报说触发」。顺序写死是为了同一天重跑时 prompt 不抖。
    """
    return list(session.scalars(
        select(Signal).where(Signal.judgment_id == judgment_id,
                             Signal.trade_date == d.isoformat())
        .order_by(Signal.rule_type, Signal.rule_id)
    ).all())


def compose_daily(session: Session, variety: str, d: date, *, researcher_id: int,
                  provider: LlmProvider | None = None, retries: int = 1) -> DailyBrief:
    """出 d 日的简报草稿并落库。同一 研究员×品种×日 幂等（由 `store.save` 保证）。

    `provider` 省略时按 `TIN_LLM_PROVIDER` 构造；测试传 `FakeProvider`，
    换内网 30B 也只是换这一个对象，业务代码一行不动。
    """
    judgment = current_judgment(session, variety)
    if judgment is None:
        # 没有 L1 骨架就只剩一堆数字，LLM 只能写出"正确的废话"（§6.3 的头号失败模式）。
        # 与其出一份差的，不如不出——这跟外部 API 断连时降级为「只做确定性信号」是同一条原则。
        raise ComposeError(f"{variety} 还没有任何判断，L1 硬骨架为空，不组装简报")

    items, skipped = prompt.select_playbook(
        session, researcher_id=researcher_id, variety=variety, d=d)
    signals = signals_of(session, judgment.id, d)

    bundle = prompt.build(session, variety=variety, d=d, judgment=judgment,
                          items=items, signals=signals, skipped_items=skipped)

    request = LlmRequest(
        purpose=prompt.PURPOSE, system=bundle.system, messages=bundle.messages,
        output_schema=prompt.OUTPUT_SCHEMA, schema_name=prompt.SCHEMA_NAME,
        # 简报行此刻还不存在，只能先以 None 落库，由 store.save 回填 llm_calls.brief_id
        brief_id=None,
    )
    provider = provider or build_provider()
    result = guard_compose(
        session, provider, request,
        # 集合直接取自 bundle，不另行拼一份：两处各拼各的，迟早有一天对不上，
        # 而对不上的那一天闸门就不再拦幻觉了。
        EvidenceContext(playbook_item_ids=set(bundle.playbook_item_ids),
                        signal_ids=set(bundle.signal_ids)),
        retries=retries,
    )

    return store.save(session, result, researcher_id=researcher_id, variety=variety,
                      trade_date=d, model=getattr(provider, "model", None),
                      signal_ids=bundle.signal_ids)
