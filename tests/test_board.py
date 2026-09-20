"""看板组装：涨跌、阈值测距、期限结构、缺口判定。"""

from datetime import datetime

from tests.conftest import D, enter_spot
from tin.compute.engine import compute_day
from tin.config import SHANGHAI
from tin.export.board import contract_curve, core_cards, expected_date, gaps, threshold_radar, ticker
from tin.jobs.seed import seed_judgment
from tin.models import Indicator


def cards(session):
    return {c.key: c for c in core_cards(session, "SN", D)}


def test_change_compares_against_previous_observation(loaded):
    """9/18 结算价 405,360 对 9/17 的 399,630（即交易所公布的昨结）。"""
    from tin.ingest.record import record
    from tin.schemas.caliber import Caliber
    from tin.schemas.observation import ObservationIn
    record(loaded, ObservationIn(
        series_id="SHFE.SN.main.settle", value=399630,
        as_of=datetime(2026, 9, 17, 15, tzinfo=SHANGHAI),
        caliber=Caliber(time_type="交易时点", price_type="结算价", contract_kind="主力合约",
                        contract="SN2610", note="按当日持仓量最大选取"),
        source="SHFE"))
    loaded.commit()
    main = cards(loaded)["main"]
    assert main.change == 405360 - 399630
    assert round(main.change_pct, 2) == round((405360 - 399630) / 399630 * 100, 2)


def test_change_absent_without_history(session):
    """只有一个数据点时不能编出涨跌。"""
    from tin.ingest.runner import store
    from tin.ingest.shfe import parse_daily_warrant
    from tests.conftest import FIX
    store(session, parse_daily_warrant((FIX / "shfe_dailystock_20260918.html").read_text(encoding="utf-8"), D))
    session.commit()
    assert cards(session)["warrant"].change is None


def test_radar_measures_distance_without_judging_safety(loaded):
    seed_judgment(loaded)
    loaded.commit()
    rows = {r["id"]: r for r in threshold_radar(loaded, "SN", D)}

    vix = rows["th_risk_vix"]
    assert vix["state"] == "数据缺失", "夹具里没有 VIX，不能假装未触发"

    entry = rows["th_entry_40w"]  # 40–41 万区间，主力结算价 405,360 落在区间内
    assert (entry["state"], entry["value"]) == ("在区间内", 405360)
    assert entry["distance"] == "已进入区间"
    assert "安全" not in entry["distance"], "只陈述距离，不给安全与否的结论"


def test_radar_reports_missing_series_rather_than_skipping(loaded):
    seed_judgment(loaded)
    loaded.commit()
    assert len(threshold_radar(loaded, "SN", D)) == 2


def test_curve_marks_main_and_thin_contracts(loaded):
    curve = contract_curve(loaded, "SN", D)
    by_contract = {r["contract"]: r for r in curve["rows"]}
    assert by_contract["SN2610"]["main"] is True
    assert by_contract["SN2610"]["active"] is True
    assert by_contract["SN2703"]["active"] is False, "成交 297 手低于 500，应标为低流动性"
    assert all("x" in r and "y" in r for r in curve["rows"]), "每个合约要有画点坐标"
    assert curve["shape"] in ("Contango", "Backwardation")


def test_ticker_only_carries_available_numbers(loaded):
    labels = [t["label"] for t in ticker(loaded, "SN", D)]
    assert "主力合约结算价" in labels
    assert "现货价" not in labels, "现货未录入时不进行情条"


def test_overseas_sources_are_not_due_on_the_same_day(session):
    """美国数据在北京时间当日尚未发布，不应记为缺口。"""
    fred = Indicator(series_id="FRED.TEST", name="测试", variety="COMMON", category="宏观",
                     caliber={"time_type": "交易时点"}, unit="%", source="FRED", frequency="日",
                     fetch_mode="auto", phase="P1", status="可用")
    session.add(fred)
    session.flush()
    assert expected_date(fred, D) < D
    assert expected_date(session.get(Indicator, "SHFE.SN.warrant"), D) == D


def test_spot_gap_offers_manual_entry(loaded):
    enter_spot(loaded, 401000, datetime(2026, 9, 17, 11, 30, tzinfo=SHANGHAI))
    compute_day(loaded, D)
    gap = next(g for g in gaps(loaded, "SN", D) if g["series_id"] == "SMM.SN.spot.1")
    assert gap["fetch_mode"] == "manual" and gap["last_as_of"] == "2026-09-17"
