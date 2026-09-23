"""每日简报组装（brief/compose.py + brief/prompt.py）。全部走 FakeProvider：不联网、不要密钥。

这些用例护四件事，每件都对应 SPEC 里一条会真正出事的约束：
- **缓存纪律**（§4.1）—— system 前缀逐字节稳定，否则 cache_read_tokens 长期为 0，成本翻十倍；
- **冷启动**（§2.1）—— playbook 为空仍要出得来简报，否则新研究员第一天就不会有第二天；
- **闸门不留洞**（§6.1 幻觉率 = 0）—— 没喂进 prompt 的条目不得出现在 EvidenceContext 里；
- **落库唯一通路** —— 组装层不许绕过 store.save 自己写 DailyBrief。
"""

from datetime import date, datetime, timezone

import pytest

from tests.conftest import D
from tests.test_signals import activate, enter
from tin.brief import compose, prompt, store
from tin.compute.signals import evaluate
from tin.config import SHANGHAI
from tin.jobs.seed import seed_researcher
from tin.judgments.service import current as current_judgment
from tin.llm.fake import FakeProvider
from tin.models import PlaybookItem

MAIN = "SHFE.SN.main.settle"
MAIN_VALUE = 405360.0                       # loaded 夹具里 9/18 的真实结算价
MAIN_AS_OF = "2026-09-18T15:00:00+08:00"


# ---------------------------------------------------------------- 夹具与小工具

@pytest.fixture
def me(session):
    return seed_researcher(session)


@pytest.fixture
def ready(loaded, me):
    """9/18 的真实行情 + 一条生效判断 + 一个研究员。简报组装的最小可跑环境。"""
    activate(loaded)
    return loaded


def item(session, researcher_id, *, id_: int, text: str, priority: int = 100,
         scope: dict | None = None, status: str = "生效") -> PlaybookItem:
    now = datetime.now(timezone.utc)
    row = PlaybookItem(id=id_, researcher_id=researcher_id, variety="SN", text=text,
                       priority=priority, scope=scope or {}, status=status,
                       created_at=now, updated_at=now)
    session.add(row)
    session.commit()
    return row


def claim(text="主力结算价落在补库区间", *, series_id=MAIN, value=MAIN_VALUE, as_of=MAIN_AS_OF,
          playbook_item_id=None, signal_id=None, side="bull") -> dict:
    evidence: dict = {}
    if series_id is not None:
        evidence["series_id"] = series_id
    if value is not None:
        evidence["value"] = value
    if as_of is not None:
        evidence["as_of"] = as_of
    if playbook_item_id is not None:
        evidence["playbook_item_id"] = playbook_item_id
    if signal_id is not None:
        evidence["signal_id"] = signal_id
    return {"text": text, "side": side, "evidence": [evidence]}


def draft(*claims, **kw) -> dict:
    payload = {
        "tone": "区间震荡",
        "stance": "逢低做多",
        "summary": "上方缺新驱动，下方有补库支撑，区间思路。",
        "claims": list(claims) or [claim()],
        "used_playbook_item_ids": [],
    }
    payload.update(kw)
    return payload


def run(session, provider, d=D, *, researcher_id):
    return compose.compose_daily(session, "SN", d, researcher_id=researcher_id,
                                 provider=provider)


def system_text(provider) -> str:
    return "\n".join(b.text for b in provider.requests[0].system)


def user_text(provider) -> str:
    return provider.requests[0].messages[0].content


# ------------------------------------------------------------------ 冷启动

def test_cold_start_with_an_empty_playbook_still_produces_a_brief(ready, me):
    """新研究员进来 L2 是空的。只靠 L1 骨架也必须出得来简报——这是 §2.1 的现实。"""
    provider = FakeProvider([draft()])

    brief = run(ready, provider, researcher_id=me.id)

    assert (brief.status, brief.tone, brief.stance) == ("草稿", "区间震荡", "逢低做多")
    assert brief.playbook_item_ids == []
    assert brief.model == "fake-model"
    assert "还没有生效的思路条目" in system_text(provider), "要明说没有个人思路，免得模型拿套话冒充"
    assert "硬骨架" in system_text(provider), "冷启动时 L1 是唯一的框架，不能也缺席"


def test_no_judgment_means_no_brief_rather_than_a_bad_one(loaded, me):
    """没有 L1 骨架就只剩一堆数字，出来的必然是「正确的废话」。宁可不出。"""
    provider = FakeProvider([draft()])
    with pytest.raises(compose.ComposeError, match="硬骨架"):
        run(loaded, provider, researcher_id=me.id)
    assert provider.calls == 0, "前置条件不成立就不该花掉一次调用"


# ---------------------------------------------------------------- scope 筛选

def test_scope_keeps_only_the_items_that_apply_today(ready, me):
    """9 月这天：始终适用的进；只在 1 月适用的不进；VIX 条件不成立的不进。"""
    enter(ready, "MACRO.VIX", 15.2, datetime(2026, 9, 18, 5, tzinfo=SHANGHAI))
    item(ready, me.id, id_=1, text="锡看盘先看 SPX 再看产业")
    item(ready, me.id, id_=2, text="春节前后按季节性看", scope={"months": [1, 2]})
    item(ready, me.id, id_=3, text="VIX 上 19 先降敞口",
         scope={"series_id": "MACRO.VIX", "when": "value > 19"})
    item(ready, me.id, id_=4, text="已停用的老思路", status="停用")

    kept, skipped = prompt.select_playbook(ready, researcher_id=me.id, variety="SN", d=D)

    assert [i.id for i in kept] == [1]
    assert {s["id"] for s in skipped} == {2, 3}, "停用的条目压根不该进筛选，更不该算「今日不适用」"
    assert "不满足" in dict((s["id"], s["reason"]) for s in skipped)[3]


def test_items_filtered_out_never_reach_the_prompt_or_the_gate(ready, me):
    """不适用的条目既不进 prompt，也不进 EvidenceContext——闸门就是靠后者拦幻觉引用的。

    LLM 引用 2 号条目（库里真实存在、只是今天不适用），必须被当成伪造引用打掉。
    """
    item(ready, me.id, id_=1, text="锡看盘先看 SPX 再看产业")
    item(ready, me.id, id_=2, text="春节前后按季节性看", scope={"months": [1, 2]})
    forged = claim("按季节性今天应当偏强", playbook_item_id=2, series_id=None, value=None, as_of=None)
    provider = FakeProvider([draft(claim(), forged), draft(claim(), forged)])

    brief = run(ready, provider, researcher_id=me.id)

    text = system_text(provider)
    assert "#1" in text and "#2" not in text, "今天不适用的条目一个字都不该出现在 prompt 里"
    assert [c["text"] for c in brief.claims] == ["主力结算价落在补库区间"]
    assert "没有喂进 prompt 的思路条目 #2" in brief.unverified[0]["reasons"][0]


def test_unknown_scope_keys_are_excluded_instead_of_silently_applied(ready, me):
    """scope 键写错了却照常每天进 prompt，等于用一条谁也没审过的规则污染简报。"""
    item(ready, me.id, id_=1, text="拼错了键名的条目", scope={"seires_id": "MACRO.VIX"})
    kept, skipped = prompt.select_playbook(ready, researcher_id=me.id, variety="SN", d=D)
    assert kept == [] and "无法判定的键" in skipped[0]["reason"]


def test_scope_condition_that_cannot_be_evaluated_is_not_applied(ready, me):
    """VIX 今天没数：条件算不出来只能说「判不了」，不能当成成立。"""
    item(ready, me.id, id_=1, text="VIX 上 19 先降敞口",
         scope={"series_id": "MACRO.VIX", "when": "value > 19"})
    _kept, skipped = prompt.select_playbook(ready, researcher_id=me.id, variety="SN", d=D)
    assert "无法判定" in skipped[0]["reason"]


def test_scope_condition_that_holds_lets_the_item_in(ready, me):
    enter(ready, "MACRO.VIX", 21.4, datetime(2026, 9, 18, 5, tzinfo=SHANGHAI))
    item(ready, me.id, id_=1, text="VIX 上 19 先降敞口",
         scope={"series_id": "MACRO.VIX", "when": "value > 19"})
    kept, _skipped = prompt.select_playbook(ready, researcher_id=me.id, variety="SN", d=D)
    assert [i.id for i in kept] == [1]


# ---------------------------------------------------------------- 缓存纪律

def test_system_prefix_is_byte_identical_between_two_runs_on_the_same_day(ready, me):
    """同一天跑两次（每天两班取数就是这个场景），前缀必须逐字节相同，否则缓存全部作废。"""
    item(ready, me.id, id_=1, text="锡看盘先看 SPX 再看产业", priority=10)
    provider = FakeProvider([draft(), draft()])

    run(ready, provider, researcher_id=me.id)
    run(ready, provider, researcher_id=me.id)

    first, second = provider.requests[0].system, provider.requests[1].system
    assert first == second
    assert [b.cache_breakpoint for b in first] == [False, False, True], "断点打在 playbook 之后"


def test_only_the_messages_change_when_the_day_changes(ready, me):
    """换一天：system 仍然一字不动，变的只有 messages。这是缓存命中的全部前提。"""
    item(ready, me.id, id_=1, text="锡看盘先看 SPX 再看产业")
    provider = FakeProvider([draft(), draft()])

    run(ready, provider, researcher_id=me.id)
    run(ready, provider, date(2026, 9, 17), researcher_id=me.id)

    assert provider.requests[0].system == provider.requests[1].system
    assert provider.requests[0].messages != provider.requests[1].messages
    assert "2026-09-18" in provider.requests[0].messages[0].content
    assert "2026-09-17" in provider.requests[1].messages[0].content


def test_todays_facts_never_leak_into_the_system_prefix(ready, me):
    """当日事实一律在 messages。日期、当前值、信号状态出现在 system 里就说明分层破了。"""
    enter(ready, "MACRO.VIX", 21.4, datetime(2026, 9, 18, 5, tzinfo=SHANGHAI))
    item(ready, me.id, id_=1, text="锡看盘先看 SPX 再看产业")
    provider = FakeProvider([draft()])

    run(ready, provider, researcher_id=me.id)

    text = system_text(provider)
    assert "2026-09-18" not in text and "405360" not in text
    assert "405360" in user_text(provider), "可引用事实必须原样摆在 messages 里"
    assert "展示口径" not in user_text(provider), \
        "VIX 已在可引用事实表里，不能再以换算后的展示口径出现第二遍——抄哪个都可能被闸门打掉"
    assert "th_entry_40w" in text, "阈值的**定义**是稳定的，该留在 system"
    assert "th_entry_40w：" not in text and "21.4" not in text, \
        "有没有触发、当前值多少都是当日事实，只能在 messages"


def test_fixed_dates_written_into_the_judgment_do_not_break_the_prefix_guard(ready, me):
    """判断里写死的日期（调研时点）不随天变，不该把整份 prompt 逼出缓存，也不该让组装崩掉。"""
    judgment = current_judgment(ready, "SN")
    judgment.survey_notes = "2026-08-16 华南调研：下游 40 万以下才补货"
    ready.commit()
    provider = FakeProvider([draft()])

    run(ready, provider, researcher_id=me.id)

    assert "2026年8月16日" in system_text(provider)
    assert not provider.requests[0].allow_volatile_system, "逃生口不许在正常路径上用"


# ------------------------------------------------------------ 条目顺序稳定

def test_playbook_order_follows_priority_then_id_not_insert_order(ready, me):
    """顺序一旦随插入先后飘，前缀就每天都不一样。排序键固定为 (priority, id)。"""
    item(ready, me.id, id_=3, text="第三条", priority=50)
    item(ready, me.id, id_=1, text="第一条", priority=50)
    item(ready, me.id, id_=2, text="第二条", priority=10)
    provider = FakeProvider([draft()])

    run(ready, provider, researcher_id=me.id)

    text = system_text(provider)
    assert text.index("#2") < text.index("#1") < text.index("#3"), "先按 priority，同 priority 按 id"


def test_the_rendered_playbook_block_is_a_pure_function_of_the_items(ready, me):
    """同一批条目、任意排列，渲染结果必须完全一致——它是缓存前缀的一部分。"""
    rows = [item(ready, me.id, id_=i, text=f"第 {i} 条", priority=p)
            for i, p in ((3, 50), (1, 50), (2, 10))]
    ordered, _ = prompt.select_playbook(ready, researcher_id=me.id, variety="SN", d=D)
    assert prompt.playbook_block(ordered) == prompt.playbook_block(
        sorted(rows, key=lambda i: (i.priority, i.id)))


# ------------------------------------------------------ 证据闸门与落库

def test_signal_ids_are_citable_and_carry_the_computed_state(ready, me):
    """阈值有没有触发是算出来的：signals 摆进 messages，LLM 只能引用它，不能自己重判。"""
    judgment = current_judgment(ready, "SN")
    assert compose.signals_of(ready, judgment.id, D) == [], "夹具此刻还没跑 evaluate_signals"

    evaluate(ready, "SN", D)
    hit = {s.rule_id: s for s in compose.signals_of(ready, judgment.id, D)}["th_entry_40w"]
    provider = FakeProvider([draft(claim("区间下沿已被触及", signal_id=hit.id,
                                         series_id=None, value=None, as_of=None))])

    brief = run(ready, provider, researcher_id=me.id)

    assert f"#{hit.id} [阈值] th_entry_40w：触发" in user_text(provider)
    assert brief.unverified == [] and hit.id in brief.signal_ids
    assert len(brief.signal_ids) == 4, "两条阈值 + 两条证伪，喂进去的和记下来的必须一致"


def test_invented_evidence_is_greyed_out_but_the_brief_still_lands(ready, me):
    """重试仍不过 → 坏论断降级进 unverified，简报照常落库。整份丢弃等于今天没有简报。"""
    bogus = claim("进口窗口打开", series_id="SMM.NI.ore.fantasy", value=123.0, as_of=MAIN_AS_OF)
    payload = draft(claim(), bogus)
    provider = FakeProvider([payload, payload])

    brief = run(ready, provider, researcher_id=me.id)

    assert provider.calls == 2, "打回重试一次"
    assert [c["text"] for c in brief.claims] == ["主力结算价落在补库区间"]
    assert [u["claim"] for u in brief.unverified] == ["进口窗口打开"]
    assert brief.tone == "区间震荡", "简报本体还在"


def test_a_value_that_disagrees_with_the_database_never_reaches_the_brief(ready, me):
    """series_id 真实存在但数值是编的——最危险的一类幻觉，光查引用拦不住。"""
    wrong = claim("主力结算价 41 万", value=410000.0)
    provider = FakeProvider([draft(wrong), draft(wrong)])

    brief = run(ready, provider, researcher_id=me.id)

    assert brief.claims == []
    assert "库内值是 405360.0" in brief.unverified[0]["reasons"][0]


def test_saving_goes_through_the_store_so_a_reviewed_brief_is_not_clobbered(ready, me):
    """组装层不许绕过 store.save：绕过去，下午那班就会把上午研究员的修改悄悄抹掉。"""
    brief = run(ready, FakeProvider([draft()]), researcher_id=me.id)
    store.review(ready, brief.id, actor="张三", action="改",
                 changes={"tone": "偏空"}, reason="宏观风险上来了")

    with pytest.raises(store.BriefError, match="已被"):
        run(ready, FakeProvider([draft()]), researcher_id=me.id)

    assert store.current(ready, me.id, "SN", D).tone == "偏空"


def test_playbook_usage_is_counted_when_the_item_is_actually_used(ready, me):
    """§6.3 要监测「连续 30 天没被引用的条目」，这个计数是唯一的数据来源。"""
    row = item(ready, me.id, id_=1, text="锡看盘先看 SPX 再看产业")
    used = claim("先看 SPX 再看产业", playbook_item_id=1, series_id=None, value=None, as_of=None)
    provider = FakeProvider([draft(claim(), used, used_playbook_item_ids=[1])])

    brief = run(ready, provider, researcher_id=me.id)

    ready.refresh(row)
    assert brief.playbook_item_ids == [1] and row.used_count == 1
