import json
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from tin.ingest.cboe import parse_vix
from tin.ingest.cfets import parse_ccpr
from tin.ingest.shfe import ParseError, parse_daily_warrant, parse_quotes, parse_weekly_stock

FIX = Path(__file__).parent / "fixtures"
D = date(2026, 9, 18)


def _by_id(batch):
    return {o.series_id: o for o in batch.observations}


@pytest.fixture(scope="module")
def quotes():
    return parse_quotes(json.loads((FIX / "shfe_kx_20260918.json").read_text()))


def test_quotes_contract_prices_match_exchange(quotes):
    obs = _by_id(quotes)
    assert obs["SHFE.SN.2610.close"].value == 407490
    assert obs["SHFE.SN.2610.settle"].value == 405360
    assert obs["SHFE.SN.2610.volume"].value == 89968
    assert obs["SHFE.SN.2610.oi"].value == 27908


def test_quotes_price_types_are_not_conflated(quotes):
    obs = _by_id(quotes)
    assert obs["SHFE.SN.2610.close"].caliber.price_type == "收盘价"
    assert obs["SHFE.SN.2610.settle"].caliber.price_type == "结算价"
    assert not any(sid.endswith(".last") for sid in obs), "盘后数据里没有最新价，不能挂 .last"


def test_quotes_as_of_is_trading_close_not_publish_time(quotes):
    o = _by_id(quotes)["SHFE.SN.2610.settle"]
    assert o.as_of == datetime(2026, 9, 18, 7, 0, tzinfo=timezone.utc)  # 15:00 +08:00


def test_quotes_main_contract_is_max_open_interest(quotes):
    main = _by_id(quotes)["SHFE.SN.main.settle"]
    assert main.value == 405360
    assert main.caliber.contract == "SN2610"
    assert main.caliber.contract_kind == "主力合约"


def test_quotes_skip_subtotal_row_and_never_zero_fill():
    payload = json.loads((FIX / "shfe_kx_20260918.json").read_text())
    for r in payload["o_curinstrument"]:
        if r["PRODUCTID"] == "sn_f" and r["DELIVERYMONTH"] == "2709":
            r["CLOSEPRICE"] = ""
    batch = parse_quotes(payload)
    ids = _by_id(batch)
    assert "SHFE.SN.2709.close" not in ids
    assert "SHFE.SN.2709.close 当日无值" in batch.skipped
    assert not any("小计" in sid for sid in ids)
    assert all(o.value != 0 for o in batch.observations if o.series_id.endswith((".close", ".settle")))


def test_warrant_daily_total():
    b = parse_daily_warrant((FIX / "shfe_dailystock_20260918.html").read_text(encoding="utf-8"), D)
    o = _by_id(b)["SHFE.SN.warrant"]
    assert o.value == 4979
    assert o.caliber.stock_scope == "交易所注册仓单"


def test_warrant_does_not_match_lead_table_via_wuxi_warehouse():
    # “中储无锡”仓库名里含“锡”字，出现在铅/锌表中；必须按表头首格精确匹配
    b = parse_daily_warrant((FIX / "shfe_dailystock_20260918.html").read_text(encoding="utf-8"), D)
    assert _by_id(b)["SHFE.SN.warrant"].value != 53115


def test_warrant_rejects_stale_page():
    with pytest.raises(ParseError, match="报表日期不符"):
        parse_daily_warrant((FIX / "shfe_dailystock_20260918.html").read_text(encoding="utf-8"), date(2026, 9, 17))


def test_weekly_stock_total_and_consistent_with_daily_warrant():
    b = parse_weekly_stock((FIX / "shfe_weeklystock_20260918.html").read_text(encoding="utf-8"), D)
    o = _by_id(b)["SHFE.SN.stock.weekly"]
    assert o.value == 7059
    assert o.caliber.stock_scope == "交易所库存"


def test_cfets_usdcny_mid():
    o = _by_id(parse_ccpr(json.loads((FIX / "cfets_ccpr_20260918.json").read_text())))["FX.USDCNY.mid"]
    assert o.value == 6.7521
    assert o.as_of == datetime(2026, 9, 18, 1, 15, tzinfo=timezone.utc)  # 09:15 +08:00
    assert o.caliber.time_type == "发布时点"


def test_vix_close_in_new_york_time():
    b = parse_vix((FIX / "cboe_vix_tail.csv").read_text(), since=date(2026, 9, 17))
    o = _by_id(b)["MACRO.VIX"]
    assert o.value == 15.44
    assert o.as_of == datetime(2026, 9, 17, 20, 15, tzinfo=timezone.utc)  # 16:15 EDT
