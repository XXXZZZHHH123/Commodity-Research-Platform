"""端到端：入库闸门、派生计算、看板组装、快照——三条不变量在真实数据上的行为。"""

from datetime import datetime

import pytest
from pydantic import ValidationError
from sqlalchemy.exc import IntegrityError

from tests.conftest import D, enter_spot
from tin.compute.engine import compute_day
from tin.config import SHANGHAI
from tin.export.board import core_cards, gaps, snapshot
from tin.ingest.record import RecordError, record
from tin.schemas.caliber import Caliber
from tin.schemas.observation import ObservationIn


def cards(session):
    return {c.key: c for c in core_cards(session, "SN", D)}


# ---------- 不变量 1：每个数字带口径与时点 ----------

def test_rejects_naive_datetime():
    with pytest.raises(ValidationError):
        ObservationIn(series_id="MACRO.VIX", value=15, as_of=datetime(2026, 9, 18, 16, 15),
                      caliber=Caliber(time_type="交易时点", price_type="收盘价"), source="CBOE")


def test_rejects_unregistered_series(session):
    with pytest.raises(RecordError, match="未登记"):
        record(session, ObservationIn(series_id="FOO.BAR", value=1, as_of=datetime(2026, 9, 18, tzinfo=SHANGHAI),
                                      caliber=Caliber(time_type="交易时点"), source="x"))


def test_rejects_caliber_that_contradicts_registration(session):
    with pytest.raises(RecordError, match="口径不符"):
        record(session, ObservationIn(
            series_id="SHFE.SN.main.settle", value=405360, as_of=datetime(2026, 9, 18, 15, tzinfo=SHANGHAI),
            caliber=Caliber(time_type="交易时点", price_type="收盘价", contract_kind="主力合约"), source="SHFE"))


def test_manual_entry_requires_person_and_source(session):
    with pytest.raises(RecordError, match="录入人与来源说明"):
        record(session, ObservationIn(
            series_id="SMM.SN.spot.1", value=406000, as_of=datetime(2026, 9, 18, 11, 30, tzinfo=SHANGHAI),
            caliber=Caliber(time_type="发布时点", price_type="均价", spot_source="SMM"), source="人工"))


def test_refetch_same_value_is_noop_and_changed_value_is_new_revision(loaded):
    kw = dict(series_id="SHFE.SN.warrant", as_of=datetime(2026, 9, 18, 15, tzinfo=SHANGHAI),
              caliber=Caliber(time_type="交易时点", stock_scope="交易所注册仓单"), source="SHFE")
    assert record(loaded, ObservationIn(value=4979, **kw)) is None
    assert record(loaded, ObservationIn(value=4980, **kw)).revision == 1


# ---------- 不变量 2：口径 / 时点不一致时不出数 ----------

def test_acceptance_previous_day_spot_against_today_future_is_blocked(loaded):
    """01 §7.1：人为制造时点错配 → 拒绝出数并提示原因。"""
    enter_spot(loaded, 401000, datetime(2026, 9, 17, 11, 30, tzinfo=SHANGHAI))
    compute_day(loaded, D)
    basis = cards(loaded)["basis"]
    assert basis.status == "blocked"
    assert basis.value is None
    assert "不在同一交易日" in basis.note
    roles = {i["role"]: i for i in basis.detail["inputs"]}
    assert roles["现货"]["as_of"] == "2026-09-17T11:30:00+08:00"
    assert roles["期货"]["as_of"] == "2026-09-18T15:00:00+08:00", "输入时点须按北京时间展示，不能露出 UTC"


def test_basis_computes_once_same_day_spot_is_entered(loaded):
    enter_spot(loaded, 406000, datetime(2026, 9, 18, 11, 30, tzinfo=SHANGHAI))
    compute_day(loaded, D)
    basis = cards(loaded)["basis"]
    assert (basis.status, basis.value) == ("ok", 406000 - 405360)
    assert basis.as_of == "2026-09-18 11:30"


def test_spread_and_warrant_change_on_real_data(loaded):
    compute_day(loaded, D)
    c = cards(loaded)
    assert (c["spread"].status, c["spread"].value) == ("ok", 407490 - 407870)
    assert c["warrant"].value == 4979 and c["warrant"].sub == "日变动 −242 吨"


def test_warrant_change_blocked_when_a_trading_day_is_missing(loaded):
    from tin.models import TradingDay
    loaded.get(TradingDay, "2026-09-17").year_seq = 172  # 模拟 17 日行情缺数：期号跳号
    loaded.commit()
    compute_day(loaded, D)
    assert cards(loaded)["warrant"].sub == "日变动：口径不一致，故不计算"


def test_missing_spot_shows_no_value_and_no_stale_value(loaded):
    enter_spot(loaded, 401000, datetime(2026, 9, 17, 11, 30, tzinfo=SHANGHAI))
    spot = cards(loaded)["spot"]
    assert spot.status == "missing" and spot.value is None
    assert spot.expected_by == "2026-09-18"
    assert any(g["series_id"] == "SMM.SN.spot.1" and g["reason"] == "人工录入未更新" for g in gaps(loaded, "SN", D))


# ---------- 快照 ----------

def test_snapshot_has_contract_fields(loaded):
    compute_day(loaded, D)
    snap = snapshot(loaded, "SN", D)
    assert set(snap) >= {"variety", "as_of", "judgment", "observations", "derived", "signals", "gaps", "surveys"}
    ids = {o["series_id"] for o in snap["observations"]}
    assert {"SHFE.SN.main.settle", "SHFE.SN.2610.close", "SHFE.SN.warrant"} <= ids
    assert all(o["caliber"] and o["as_of"] for o in snap["observations"])
    blocked = [d for d in snap["derived"] if d["status"] != "ok"]
    assert all(d["value"] is None for d in blocked)


def test_fk_is_enforced_at_database_level(session):
    """证伪绑定指标的最后一道防线：即便绕过服务层直接写引用表，数据库也拒绝未登记指标。"""
    from tin.jobs.seed import seed_judgment
    from tin.models import JudgmentSeriesRef
    j = seed_judgment(session)
    session.add(JudgmentSeriesRef(judgment_id=j.id, ref_type="falsifier", ref_id="ok", series_id="MACRO.VIX"))
    session.flush()
    session.add(JudgmentSeriesRef(judgment_id=j.id, ref_type="falsifier", ref_id="x", series_id="NOT.REGISTERED"))
    with pytest.raises(IntegrityError, match="FOREIGN KEY"):
        session.flush()
