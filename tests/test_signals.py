"""L1 事实 × L2 判断的确定性比对：阈值测距、证伪连续期、数据缺失、幂等与待办去重。

这一层不经过 LLM，所以每条断言都可以写死期望值——这正是把它和简报层分开的理由。
"""

import json
from datetime import date, datetime

from sqlalchemy import select

from tests.conftest import D
from tin.compute.signals import create_review_tasks, evaluate, evaluate_signals
from tin.config import ROOT, SHANGHAI
from tin.ingest.record import record
from tin.judgments.service import save_version
from tin.models import Indicator, Judgment, ReviewTask, Signal
from tin.schemas.caliber import Caliber
from tin.schemas.judgment import JudgmentIn
from tin.schemas.observation import ObservationIn

SEED = json.loads((ROOT / "seeds" / "judgment_sn_v1.json").read_text(encoding="utf-8"))
PRIORITY = {"order": ["th_risk_vix", "th_entry_40w"], "note": "风控线优先"}


def activate(session, **over) -> Judgment:
    """用种子判断建一条生效判断。

    不走 jobs.seed.seed_judgment：它要读仓库里没有的 PRD 原文，测试不该依赖那个文件。
    """
    row = save_version(session, JudgmentIn.model_validate({**SEED, "priority_rule": PRIORITY, **over}),
                       actor="测试", activate=True)
    session.commit()
    return row


def enter(session, series_id: str, value: float, when: datetime):
    """按指标登记的口径录一条观测，避免测试自己编口径被入库闸门拦下。"""
    ind = session.get(Indicator, series_id)
    record(session, ObservationIn(series_id=series_id, value=value, as_of=when,
                                  caliber=Caliber.model_validate(ind.caliber), source="测试",
                                  entered_by="测试", note="测试录入"))
    session.commit()


def week(day: int) -> datetime:
    return datetime(2026, 9, day, 10, tzinfo=SHANGHAI)


def by_rule(signals) -> dict[str, Signal]:
    return {s.rule_id: s for s in signals}


# ---------- 阈值 ----------

def test_threshold_triggers_when_line_is_crossed(loaded):
    activate(loaded)
    enter(loaded, "MACRO.VIX", 21.4, datetime(2026, 9, 18, 5, tzinfo=SHANGHAI))
    vix = by_rule(evaluate(loaded, "SN", D))["th_risk_vix"]
    assert (vix.state, vix.current_value) == ("触发", 21.4)
    assert vix.rule_type == "threshold" and vix.gap_note is None
    assert vix.evidence[0]["series_id"] == "MACRO.VIX", "触发必须挂在一个真实数据点上"
    assert vix.evidence[0]["as_of"].startswith("2026-09-18")


def test_threshold_not_triggered_below_the_line(loaded):
    activate(loaded)
    enter(loaded, "MACRO.VIX", 15.2, datetime(2026, 9, 18, 5, tzinfo=SHANGHAI))
    vix = by_rule(evaluate(loaded, "SN", D))["th_risk_vix"]
    assert (vix.state, vix.current_value) == ("未触发", 15.2)
    assert "距触发" in vix.evidence[0]["desc"], "未触发也要说明还差多远"


def test_between_range_hit_is_recorded_as_triggered(loaded):
    """40–41 万区间，主力结算价 405,360 落在区间内。

    threshold_radar 的词表里这叫「在区间内」，落到 signals 必须收敛成「触发」——
    signals 刻意只有三态，下游不该去认第四个词。
    """
    activate(loaded)
    entry = by_rule(evaluate(loaded, "SN", D))["th_entry_40w"]
    assert (entry.state, entry.current_value) == ("触发", 405360)
    assert "已进入区间" in entry.evidence[0]["desc"]


def test_between_range_miss_is_not_triggered(loaded):
    activate(loaded, thresholds=[{**SEED["thresholds"][1], "value": [300000, 310000]}],
             priority_rule=None)
    entry = by_rule(evaluate(loaded, "SN", D))["th_entry_40w"]
    assert entry.state == "未触发" and entry.current_value == 405360


def test_states_stay_within_the_three_word_vocabulary(loaded):
    """Signal 刻意没有「建议」，状态也只有三个词；这条约束由测试守住。"""
    activate(loaded)
    assert {s.state for s in evaluate(loaded, "SN", D)} <= {"未触发", "触发", "数据缺失"}


# ---------- 数据缺失 ----------

def test_missing_series_is_reported_not_skipped(loaded):
    """夹具里没有 VIX、没有缅甸进口：不能跳过，更不能当成未触发。"""
    activate(loaded)
    rows = by_rule(evaluate(loaded, "SN", D))
    assert len(rows) == 4, "两条阈值 + 两条证伪，一条都不能少"
    for rule in ("th_risk_vix", "fl_myanmar"):
        assert rows[rule].state == "数据缺失"
        assert rows[rule].current_value is None
        assert rows[rule].gap_note, "缺失必须写明缺在哪"
    assert "MACRO.VIX" in rows["th_risk_vix"].gap_note


def test_unregistered_series_is_distinguished_from_empty_series(loaded):
    """未登记是判断写错了，没数据是数据断供；两件事要分得开才知道该找谁。"""
    j = activate(loaded)
    # 服务层会拦下未登记的引用，只有绕过它写库才会出现（旧库迁入 / 指标下架）。
    j.thresholds = [{**SEED["thresholds"][0], "series_id": "NOT.REGISTERED"}]
    loaded.commit()
    vix = by_rule(evaluate(loaded, "SN", D))["th_risk_vix"]
    assert vix.state == "数据缺失" and "未登记" in vix.gap_note


# ---------- 证伪条件 ----------

def test_falsifier_expression_on_a_single_period(loaded):
    """缅甸月进口回万吨（value >= 10000，1 期即可判定）。"""
    activate(loaded)
    enter(loaded, "CUSTOMS.2609.import.MM", 12300, week(1))
    hit = by_rule(evaluate(loaded, "SN", D))["fl_myanmar"]
    assert (hit.state, hit.current_value, hit.rule_type) == ("触发", 12300, "falsifier")

    enter(loaded, "CUSTOMS.2609.import.MM", 8200, week(2))
    miss = by_rule(evaluate(loaded, "SN", D))["fl_myanmar"]
    assert (miss.state, miss.current_value) == ("未触发", 8200)


def test_falsifier_needs_enough_points_for_consecutive_periods(loaded):
    """社库连续两周累库要 3 个点才算得出 2 期变化；点不够只能说「算不出来」。"""
    activate(loaded)
    enter(loaded, "MYSTEEL.SN.stock.social", 5000, week(4))
    enter(loaded, "MYSTEEL.SN.stock.social", 5200, week(11))
    row = by_rule(evaluate(loaded, "SN", D))["fl_social_stock_2w"]
    assert row.state == "数据缺失"
    assert "需要 3 个数据点" in row.gap_note and "只有 2 个" in row.gap_note


def test_falsifier_not_triggered_when_the_streak_breaks(loaded):
    """一周累、一周去：连续期中断就要从头数，不能凑成「连续两周」。"""
    activate(loaded)
    for day, v in ((4, 5000), (11, 5200), (18, 5100)):
        enter(loaded, "MYSTEEL.SN.stock.social", v, week(day))
    row = by_rule(evaluate(loaded, "SN", D))["fl_social_stock_2w"]
    assert row.state == "未触发"
    assert row.current_value == -100, "current_value 是最后一期的变化量，不是水平值"
    assert [e["note"].endswith("不成立") for e in row.evidence] == [False, False, True]


def test_falsifier_triggers_after_two_consecutive_periods(loaded):
    activate(loaded)
    for day, v in ((4, 5000), (11, 5200), (18, 5450)):
        enter(loaded, "MYSTEEL.SN.stock.social", v, week(day))
    row = by_rule(evaluate(loaded, "SN", D))["fl_social_stock_2w"]
    assert (row.state, row.current_value) == ("触发", 250)
    base = row.evidence[0]
    assert "基期" in base["desc"] and "不参与判定" in base["note"]
    assert len(row.evidence) == 3, "基期 + 两期变化，证据链要完整到可复算"


def test_revisions_do_not_count_as_extra_periods(loaded):
    """同一时点修订三次仍然只是一期。按行数数连续期会把修订历史当成趋势。"""
    activate(loaded)
    enter(loaded, "MYSTEEL.SN.stock.social", 5000, week(4))
    for v in (5100, 5200, 5300):
        enter(loaded, "MYSTEEL.SN.stock.social", v, week(11))
    row = by_rule(evaluate(loaded, "SN", D))["fl_social_stock_2w"]
    assert row.state == "数据缺失", "两个时点 + 若干修订，仍然算不出连续两期"


# ---------- 幂等 ----------

def test_rerunning_the_same_day_overwrites_instead_of_appending(loaded):
    """每天两班取数会重算同一天；插入式写法会让一条规则一天堆出两行互相矛盾的记录。"""
    activate(loaded)
    enter(loaded, "MACRO.VIX", 15.2, datetime(2026, 9, 18, 5, tzinfo=SHANGHAI))
    first = by_rule(evaluate(loaded, "SN", D))["th_risk_vix"]
    assert first.state == "未触发"

    enter(loaded, "MACRO.VIX", 22.0, datetime(2026, 9, 18, 21, tzinfo=SHANGHAI))
    second = by_rule(evaluate(loaded, "SN", D))["th_risk_vix"]
    assert second.id == first.id, "同一 judgment+rule+trade_date 只能有一行"
    assert (second.state, second.current_value) == ("触发", 22.0), "重跑要覆盖成最新结论"
    assert len(loaded.scalars(select(Signal)).all()) == 4, "两条阈值 + 两条证伪，重跑不新增行"


def test_different_days_keep_separate_rows(loaded):
    activate(loaded)
    enter(loaded, "MACRO.VIX", 21.0, datetime(2026, 9, 17, 5, tzinfo=SHANGHAI))
    evaluate(loaded, "SN", date(2026, 9, 17))
    evaluate(loaded, "SN", D)
    dates = {s.trade_date for s in loaded.scalars(select(Signal).where(Signal.rule_id == "th_risk_vix"))}
    assert dates == {"2026-09-17", "2026-09-18"}


def test_no_judgment_means_no_signals(loaded):
    assert evaluate(loaded, "SN", D) == []


# ---------- 复盘待办 ----------

def test_falsifier_trigger_creates_one_task_per_trigger(loaded):
    activate(loaded)
    enter(loaded, "CUSTOMS.2609.import.MM", 12300, week(1))
    signals, tasks = evaluate_signals(loaded, "SN", D, today=date(2026, 9, 1))
    falsified = [t for t in tasks if t.trigger_type == "证伪触发"]
    assert len(falsified) == 1
    task = falsified[0]
    assert task.signal_id == by_rule(signals)["fl_myanmar"].id
    assert (task.assignee, task.state) == ("锡研究员", "待处理")


def test_a_standing_trigger_does_not_pile_up_tasks(loaded):
    """一条证伪连续成立十天是同一件事；上一条待办没处理就不该再堆新的。"""
    activate(loaded)
    enter(loaded, "CUSTOMS.2609.import.MM", 12300, week(1))
    for d in (date(2026, 9, 17), D):
        evaluate_signals(loaded, "SN", d, today=date(2026, 9, 1))
    evaluate_signals(loaded, "SN", D, today=date(2026, 9, 1))  # 当天重跑
    rows = loaded.scalars(select(ReviewTask).where(ReviewTask.trigger_type == "证伪触发")).all()
    assert len(rows) == 1


def test_untriggered_falsifier_creates_no_task(loaded):
    activate(loaded)
    enter(loaded, "CUSTOMS.2609.import.MM", 8200, week(1))
    _s, tasks = evaluate_signals(loaded, "SN", D, today=date(2026, 9, 1))
    assert [t.trigger_type for t in tasks] == []


def test_expired_judgment_creates_a_review_task_once(loaded):
    """写于 8/16、复盘周期 30 天 → 9/15 到期；9/18 这天必须有一条到期待办。"""
    j = activate(loaded)
    assert j.review_due == date(2026, 9, 15)
    _s, tasks = evaluate_signals(loaded, "SN", D)
    assert [t.trigger_type for t in tasks] == ["判断到期"]
    assert tasks[0].signal_id is None and tasks[0].assignee == "锡研究员"

    evaluate_signals(loaded, "SN", D)
    rows = loaded.scalars(select(ReviewTask).where(ReviewTask.trigger_type == "判断到期")).all()
    assert len(rows) == 1, "到期是一次性事件，不能每天刷一条"


def test_judgment_in_period_creates_no_expiry_task(loaded):
    activate(loaded)
    _s, tasks = evaluate_signals(loaded, "SN", D, today=date(2026, 9, 1))
    assert "判断到期" not in {t.trigger_type for t in tasks}
