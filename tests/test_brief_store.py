"""简报落库与评审的唯一入口（brief/store.py）。

重点守两件事：重跑不得抹掉研究员的修改；「改 / 否」必须留下原因——
那是蒸馏（F6）唯一的原料，空着等于这次纠错白做。
"""

from datetime import date, datetime, timezone

import pytest
from sqlalchemy import select

from tin.brief import store
from tin.jobs.seed import seed_researcher
from tin.llm.guard import GuardResult
from tin.models import BriefRevision, LlmCall, PlaybookItem
from tin.schemas.brief import BriefDraft, Claim, ClaimEvidence, PriceRange

D = date(2026, 9, 18)


def _draft(tone="区间震荡", summary="测试简报", stance="逢低做多") -> BriefDraft:
    return BriefDraft(
        tone=tone, summary=summary, stance=stance,
        price_range=PriceRange(low=400000, high=410000, series_id="SHFE.SN.main.settle"),
        claims=[Claim(text="主力结算价在补库区间内", side="bull",
                      evidence=[ClaimEvidence(series_id="SHFE.SN.main.settle", value=406000.0,
                                              as_of="2026-09-18T15:00:00+08:00")])],
    )


def _result(*, playbook_ids=(), call_ids=(), unverified=()) -> GuardResult:
    return GuardResult(draft=_draft(), unverified=list(unverified),
                       used_playbook_item_ids=list(playbook_ids), attempts=1,
                       call_ids=list(call_ids))


@pytest.fixture
def me(session):
    return seed_researcher(session)


def test_save_writes_the_draft_and_keeps_unverified_visible(session, me):
    b = store.save(session, _result(unverified=[{"text": "无证据的话", "problems": ["编的"]}]),
                   researcher_id=me.id, variety="SN", trade_date=D, model="fake-model")
    assert (b.status, b.tone, b.stance) == ("草稿", "区间震荡", "逢低做多")
    assert (b.range_low, b.range_high) == (400000, 410000)
    assert len(b.claims) == 1 and b.claims[0]["evidence"][0]["value"] == 406000.0
    assert b.unverified, "降级标灰的论断必须存下来，它是质量警报"


def test_rerunning_the_same_day_replaces_the_draft_instead_of_piling_up(session, me):
    first = store.save(session, _result(), researcher_id=me.id, variety="SN", trade_date=D)
    again = store.save(session, _result(), researcher_id=me.id, variety="SN", trade_date=D)
    assert first.id == again.id, "每天两班取数会重算同一天，不能堆出两份简报"


def test_rerun_refuses_to_clobber_a_brief_the_researcher_already_handled(session, me):
    """下午那班取数不能把上午研究员改过的简报悄悄抹掉——这种丢失不报错、没人会发现。"""
    b = store.save(session, _result(), researcher_id=me.id, variety="SN", trade_date=D)
    store.review(session, b.id, actor="张三", action="改",
                 changes={"tone": "偏空"}, reason="宏观风险上来了")
    with pytest.raises(store.BriefError, match="已被"):
        store.save(session, _result(), researcher_id=me.id, variety="SN", trade_date=D)
    assert store.current(session, me.id, "SN", D).tone == "偏空", "人的修改必须还在"


def test_save_backfills_llm_call_brief_id(session, me):
    """出简报时 daily_briefs 行还不存在，调用记录只能先以 None 落库；不回填就断了审计链。"""
    call = LlmCall(purpose="compose_brief", provider="fake", model="m", request={},
                   status="ok", created_at=datetime.now(timezone.utc))
    session.add(call)
    session.commit()
    b = store.save(session, _result(call_ids=[call.id]),
                   researcher_id=me.id, variety="SN", trade_date=D)
    session.refresh(call)
    assert call.brief_id == b.id


def test_save_counts_playbook_usage(session, me):
    """§6.3 要监测「连续 30 天没被引用的条目」，这个计数是唯一数据来源。"""
    now = datetime.now(timezone.utc)
    item = PlaybookItem(researcher_id=me.id, variety="SN", text="锡看盘先看 SPX",
                        status="生效", created_at=now, updated_at=now)
    session.add(item)
    session.commit()
    store.save(session, _result(playbook_ids=[item.id]),
               researcher_id=me.id, variety="SN", trade_date=D)
    session.refresh(item)
    assert item.used_count == 1 and item.last_used_at is not None


def test_adopt_records_a_revision_and_the_time_spent(session, me):
    b = store.save(session, _result(), researcher_id=me.id, variety="SN", trade_date=D)
    revs = store.review(session, b.id, actor="张三", action="采纳", elapsed_seconds=3)
    assert len(revs) == 1 and revs[0].action == "采纳"
    assert revs[0].elapsed_seconds == 3, "秒级采纳 = 没看，§6.3 要靠这个识别"
    assert (b.status, b.reviewed_by) == ("已采纳", "张三")


def test_each_changed_field_gets_its_own_revision(session, me):
    """一个字段一条记录：蒸馏时要能看出「这个人反复在改哪一项」。"""
    b = store.save(session, _result(), researcher_id=me.id, variety="SN", trade_date=D)
    revs = store.review(session, b.id, actor="张三", action="改",
                        changes={"tone": "偏空", "stance": "观望"}, reason="宏观转向")
    assert {r.field for r in revs} == {"tone", "stance"}
    assert all(r.reason == "宏观转向" for r in revs)
    assert (b.tone, b.stance, b.status) == ("偏空", "观望", "已修改")
    stored = session.scalars(select(BriefRevision).where(BriefRevision.brief_id == b.id)).all()
    assert len(stored) == 2


@pytest.mark.parametrize("action,changes", [("改", {"tone": "偏空"}), ("否", None)])
def test_change_and_reject_must_state_why(session, me, action, changes):
    """原因是蒸馏唯一的原料，空着这次纠错就白做了。"""
    b = store.save(session, _result(), researcher_id=me.id, variety="SN", trade_date=D)
    with pytest.raises(store.BriefError, match="原因"):
        store.review(session, b.id, actor="张三", action=action, changes=changes, reason="  ")


def test_adopt_does_not_require_a_reason(session, me):
    """没改就是没意见，逼人编一句理由只会得到一堆「同意」。"""
    b = store.save(session, _result(), researcher_id=me.id, variety="SN", trade_date=D)
    assert store.review(session, b.id, actor="张三", action="采纳")


def test_change_that_changes_nothing_is_rejected(session, me):
    b = store.save(session, _result(), researcher_id=me.id, variety="SN", trade_date=D)
    with pytest.raises(store.BriefError, match="没有实际修改"):
        store.review(session, b.id, actor="张三", action="改",
                     changes={"tone": "区间震荡"}, reason="其实没改")


def test_system_provenance_fields_cannot_be_rewritten_by_hand(session, me):
    """unverified / playbook_item_ids 是系统留痕，不是观点——让人改等于篡改审计记录。"""
    b = store.save(session, _result(), researcher_id=me.id, variety="SN", trade_date=D)
    for field in ("unverified", "playbook_item_ids", "model", "signal_ids"):
        with pytest.raises(store.BriefError, match="不可修改"):
            store.review(session, b.id, actor="张三", action="改",
                         changes={field: []}, reason="想抹掉警报")


def test_reopen_lets_a_mistakenly_rejected_brief_be_regenerated(session, me):
    """误点一次「否」不该让研究员当天再也拿不到简报——save 拒绝覆盖是对的，但要有出口。"""
    b = store.save(session, _result(), researcher_id=me.id, variety="SN", trade_date=D)
    store.review(session, b.id, actor="张三", action="否", reason="今天数据不全")
    with pytest.raises(store.BriefError):
        store.save(session, _result(), researcher_id=me.id, variety="SN", trade_date=D)

    store.reopen(session, b.id, actor="张三", reason="数据补齐了，重出一份")
    assert (b.status, b.reviewed_by, b.reviewed_at) == ("草稿", None, None)
    again = store.save(session, _result(), researcher_id=me.id, variety="SN", trade_date=D)
    assert again.id == b.id and again.status == "草稿"


def test_reopen_is_itself_recorded_and_needs_a_reason(session, me):
    """退回也写纠错日志：「他退回了什么、为什么」同样是学习信号。"""
    b = store.save(session, _result(), researcher_id=me.id, variety="SN", trade_date=D)
    store.review(session, b.id, actor="张三", action="采纳")
    with pytest.raises(store.BriefError, match="原因"):
        store.reopen(session, b.id, actor="张三", reason="   ")
    rev = store.reopen(session, b.id, actor="张三", reason="点错了")
    assert rev.reason == "点错了"
    assert session.scalars(
        select(BriefRevision).where(BriefRevision.brief_id == b.id)).all()


def test_reopening_a_draft_is_refused(session, me):
    b = store.save(session, _result(), researcher_id=me.id, variety="SN", trade_date=D)
    with pytest.raises(store.BriefError, match="不需要撤回"):
        store.reopen(session, b.id, actor="张三", reason="随便")


def test_tone_note_survives_the_round_trip(session, me):
    """「为什么是这个基调」那句话必须到得了页面——少一列就被静默丢掉。"""
    draft = _draft()
    draft.tone_note = "高位宽幅震荡，回调做多优于追涨"
    r = GuardResult(draft=draft, unverified=[], used_playbook_item_ids=[], attempts=1, call_ids=[])
    b = store.save(session, r, researcher_id=me.id, variety="SN", trade_date=D)
    assert b.tone_note == "高位宽幅震荡，回调做多优于追涨"


def test_unknown_action_is_refused(session, me):
    b = store.save(session, _result(), researcher_id=me.id, variety="SN", trade_date=D)
    with pytest.raises(store.BriefError, match="动作只能是"):
        store.review(session, b.id, actor="张三", action="点赞")
