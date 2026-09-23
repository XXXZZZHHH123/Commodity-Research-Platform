"""简报落库与评审的唯一入口。

为什么要收口：`llm/guard.py` 的证据闸门只是一段代码，谁直接往 `daily_briefs.claims`
写 JSON 就绕过去了。一期靠 `ingest/record.record()` 做成观测入库的唯一通路才让
「只存真实取到的数」这条纪律真正生效——这里照搬同一招。

组装层（brief/compose.py）不允许自己拼 DailyBrief，只能把 GuardResult 交给 `save()`。
"""

from datetime import date, datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from tin.llm.guard import GuardResult
from tin.models import BriefRevision, DailyBrief, LlmCall, PlaybookItem
from tin.models.agent import BRIEF_STATUSES, REVISION_ACTIONS
from tin.schemas.brief import BriefDraft

# 研究员动作 → 简报落到哪个状态
ACTION_STATUS = {"采纳": "已采纳", "改": "已修改", "否": "已否决"}
# 已经被研究员处理过的简报不再被重跑覆盖——覆盖等于把人的修改悄悄抹掉
SETTLED = ("已采纳", "已修改", "已否决")
# 研究员能改的字段。刻意不含 unverified / playbook_item_ids / signal_ids / model：
# 那些是系统的留痕，不是观点，让人改等于让人篡改审计记录。
EDITABLE_FIELDS = ("tone", "tone_note", "summary", "stance",
                   "range_low", "range_high", "range_series_id", "claims")


class BriefError(Exception):
    """落库或评审被规则挡下。调用方应当把原因原样呈现给研究员，不要吞掉。"""


def _draft_fields(draft: BriefDraft) -> dict:
    pr = draft.price_range
    return {
        "tone": draft.tone,
        "tone_note": draft.tone_note,
        "summary": draft.summary,
        "stance": draft.stance,
        "range_low": pr.low if pr else None,
        "range_high": pr.high if pr else None,
        "range_series_id": pr.series_id if pr else None,
        "claims": [c.model_dump() for c in draft.claims],
    }


def save(session: Session, result: GuardResult, *, researcher_id: int, variety: str,
         trade_date: date, model: str | None = None, signal_ids: list[int] | None = None) -> DailyBrief:
    """把闸门产出落成当日简报。同一 研究员×品种×交易日 幂等，重跑覆盖草稿。

    **但已被研究员处理过的简报不覆盖**：每天两班取数会重算同一天，若研究员上午已经
    改过或否掉了草稿，下午那班再跑一遍就把人的修改抹了。这种数据丢失不会报错、
    也没人会发现，所以必须在这里挡住。

    同时做三件顺手但不能漏的事：
    - 回填 `llm_calls.brief_id`：出简报时这行还不存在，调用记录只能先以 None 落库，
      不回填的话「为什么今天这么说」就断了链（§4.1 的审计前提）。
    - 累计 `playbook_items.used_count` / `last_used_at`：§6.3 要监测「连续 30 天
      没被引用的条目」，这个计数是唯一的数据来源。
    - `unverified` 非空即为质量警报，原样存下，页面要标灰。
    """
    row = session.scalars(
        select(DailyBrief).where(DailyBrief.researcher_id == researcher_id,
                                 DailyBrief.variety == variety,
                                 DailyBrief.trade_date == trade_date.isoformat())
    ).first()
    if row is not None and row.status in SETTLED:
        raise BriefError(
            f"{trade_date} 的简报已被 {row.reviewed_by or '研究员'} 处理为「{row.status}」，"
            f"不覆盖。要重新生成请先撤回（store.reopen / 页面上的「撤回」按钮）。")

    if row is None:
        row = DailyBrief(researcher_id=researcher_id, variety=variety,
                         trade_date=trade_date.isoformat())
        session.add(row)

    for k, v in _draft_fields(result.draft).items():
        setattr(row, k, v)
    row.status = "草稿"
    row.unverified = result.unverified
    row.playbook_item_ids = list(result.used_playbook_item_ids)
    row.signal_ids = list(signal_ids or [])
    row.model = model
    row.generated_at = datetime.now(timezone.utc)
    row.reviewed_at = None
    row.reviewed_by = None
    session.flush()  # 拿到主键才能回填 llm_calls.brief_id

    if result.call_ids:
        for call in session.scalars(select(LlmCall).where(LlmCall.id.in_(result.call_ids))):
            call.brief_id = row.id

    if row.playbook_item_ids:
        now = datetime.now(timezone.utc)
        for item in session.scalars(
                select(PlaybookItem).where(PlaybookItem.id.in_(row.playbook_item_ids))):
            item.used_count += 1
            item.last_used_at = now

    session.commit()
    return row


def review(session: Session, brief_id: int, *, actor: str, action: str,
           changes: dict | None = None, reason: str | None = None,
           elapsed_seconds: int | None = None) -> list[BriefRevision]:
    """研究员的「采纳 / 改 / 否」。每一次都写纠错日志——这是 agent 唯一的学习养料。

    不做公开命中率排名（SPEC §3.2）不等于可以省掉这张表：不统计人的准确率、不排名、
    不对外，但记录照常入库。省掉它 L3 反馈循环就是死的。

    `改` 和 `否` 必须给 `reason`：蒸馏（F6）的原料就是这个字段，空着等于这次修改白改。
    `采纳` 不强制——没改就是没意见，逼人编一句理由只会得到一堆「同意」。

    `elapsed_seconds` 由页面上报（从简报展示到提交的秒数），服务端算不出来：简报可能
    早上 8 点生成、下午 2 点提交，但只被看了 3 秒。§6.3 要靠它识别「秒级采纳 = 没看」
    这个失败模式，所以宁可信页面也不要用 generated_at 反推。
    """
    if action not in REVISION_ACTIONS:
        raise BriefError(f"动作只能是 {'/'.join(REVISION_ACTIONS)}，收到 {action!r}")
    brief = session.get(DailyBrief, brief_id)
    if brief is None:
        raise BriefError(f"简报 {brief_id} 不存在")
    if action in ("改", "否") and not (reason or "").strip():
        raise BriefError(f"「{action}」必须写明原因——这是 agent 学习的唯一养料，不能空着")

    changes = changes or {}
    if action == "改" and not changes:
        raise BriefError("「改」至少要改动一个字段，否则应当选「采纳」")
    if unknown := set(changes) - set(EDITABLE_FIELDS):
        raise BriefError(f"不可修改的字段：{sorted(unknown)}（系统留痕不接受人工改写）")

    now = datetime.now(timezone.utc)
    out: list[BriefRevision] = []

    def log(field: str | None, before, after):
        rev = BriefRevision(brief_id=brief.id, actor=actor, action=action, field=field,
                            before={"value": before}, after={"value": after},
                            reason=reason, elapsed_seconds=elapsed_seconds, created_at=now)
        session.add(rev)
        out.append(rev)

    if action == "改":
        # 一个字段一条记录：蒸馏时要能看出「这个人反复在改哪一项」，
        # 合成一条大 diff 就丢掉了这个信号。
        for field, after in changes.items():
            before = getattr(brief, field)
            if before == after:
                continue
            log(field, before, after)
            setattr(brief, field, after)
        if not out:
            raise BriefError("提交的改动与当前简报一致，没有实际修改")
    else:
        log(None, {"status": brief.status}, {"status": ACTION_STATUS[action]})

    brief.status = ACTION_STATUS[action]
    brief.reviewed_at = now
    brief.reviewed_by = actor
    session.commit()
    return out


def reopen(session: Session, brief_id: int, *, actor: str, reason: str) -> BriefRevision:
    """把已处理的简报退回草稿，让重跑能再次覆盖它。

    `save()` 拒绝覆盖已处理的简报是对的（防止下午那班取数抹掉上午的修改），但没有
    这个出口就成了死胡同：研究员误点一次「否」，当天就再也拿不到简报。

    退回本身也写一条纠错日志——「他把什么退回了、为什么」同样是学习信号，
    而且比悄悄把状态改回去更诚实。
    """
    brief = session.get(DailyBrief, brief_id)
    if brief is None:
        raise BriefError(f"简报 {brief_id} 不存在")
    if brief.status not in SETTLED:
        raise BriefError(f"简报当前是「{brief.status}」，本来就可以重新生成，不需要撤回")
    if not (reason or "").strip():
        raise BriefError("撤回必须写明原因")

    rev = BriefRevision(brief_id=brief.id, actor=actor, action="否",
                        field="__reopen__", before={"status": brief.status},
                        after={"status": "草稿"}, reason=reason,
                        created_at=datetime.now(timezone.utc))
    session.add(rev)
    brief.status = "草稿"
    brief.reviewed_at = None
    brief.reviewed_by = None
    session.commit()
    return rev


def current(session: Session, researcher_id: int, variety: str, d: date) -> DailyBrief | None:
    """取某人某日的简报。页面与快照都走这里，避免两处各查各的口径不一致。"""
    return session.scalars(
        select(DailyBrief).where(DailyBrief.researcher_id == researcher_id,
                                 DailyBrief.variety == variety,
                                 DailyBrief.trade_date == d.isoformat())
    ).first()


assert set(ACTION_STATUS) == set(REVISION_ACTIONS), "动作词表与状态映射必须一一对应"
assert set(ACTION_STATUS.values()) <= set(BRIEF_STATUSES), "状态映射必须落在 BRIEF_STATUSES 内"
