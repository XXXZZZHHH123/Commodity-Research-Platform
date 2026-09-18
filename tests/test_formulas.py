from datetime import datetime

import pytest

from tin.compute.formulas import basis, spread_m1m2, stock_chg_d
from tin.compute.guards import BLOCKED_CALIBER, BLOCKED_TIME, MISSING, OK, Point, Violation, same_caliber
from tin.config import SHANGHAI


def at(day: int, hh: int, mm: int = 0) -> datetime:
    return datetime(2026, 9, day, hh, mm, tzinfo=SHANGHAI)


def spot(value=406000, when=None):
    return Point("现货", "SMM.SN.spot.1", 1, value, when or at(18, 11, 30),
                 {"price_type": "均价", "spot_source": "SMM"}, "SMM")


def fut(value=405360, when=None, kind="主力合约"):
    return Point("期货", "SHFE.SN.main.settle", 2, value, when or at(18, 15),
                 {"price_type": "结算价", "contract_kind": kind, "contract": "SN2610"}, "SHFE")


def leg(role, contract, field, value, day=18):
    price_type = {"close": "收盘价", "settle": "结算价", "volume": None}[field]
    cal = {"contract_kind": "具体合约", "contract": contract}
    if price_type:
        cal["price_type"] = price_type
    return Point(role, f"SHFE.SN.{contract[2:]}.{field}", None, value, at(day, 15), cal, "SHFE")


def legs(role, contract, close, settle, volume):
    return {"close": leg(role, contract, "close", close), "settle": leg(role, contract, "settle", settle),
            "volume": leg(role, contract, "volume", volume)}


def warrant(value, day):
    return Point("仓单", "SHFE.SN.warrant", None, value, at(day, 15), {"stock_scope": "交易所注册仓单"}, "SHFE")


# ---------- 基差 ----------

def test_basis_ok_within_tolerance():
    r = basis(spot(), fut())
    assert (r.status, r.value) == (OK, 640)
    assert r.as_of == at(18, 11, 30), "派生值时点取输入中最早者"
    assert {i["role"] for i in r.inputs} == {"现货", "期货"}


def test_basis_blocks_previous_day_spot_against_today_future():
    """01 §7.1 验收：用前一日现货价配当日期货价，系统拒绝出数并提示原因。"""
    r = basis(spot(when=at(17, 11, 30)), fut())
    assert r.status == BLOCKED_TIME
    assert r.value is None
    assert "不在同一交易日" in r.note and "09-17 11:30" in r.note and "09-18 15:00" in r.note
    assert len(r.inputs) == 2, "阻断时仍要列出冲突的两个输入，供页面展示"


def test_basis_blocks_same_day_beyond_4h():
    r = basis(spot(when=at(18, 8)), fut())  # 04 §1.9 的示例：现货 08:00 vs 期货 15:00
    assert r.status == BLOCKED_TIME and r.value is None
    assert "7.0 小时" in r.note


def test_basis_rejects_vendor_continuous_series():
    r = basis(spot(), fut(kind="主力连续"))
    assert r.status == BLOCKED_CALIBER and r.value is None


def test_basis_missing_spot():
    r = basis(None, fut())
    assert r.status == MISSING and r.value is None and "现货" in r.note


# ---------- 近月−次月价差 ----------

def test_spread_uses_close_when_both_legs_liquid():
    r = spread_m1m2(legs("近月", "SN2610", 407490, 405360, 89968),
                    legs("次月", "SN2611", 407870, 405660, 37924), 500)
    assert (r.status, r.value) == (OK, 407490 - 407870)
    assert r.params["price_field"] == "close" and r.note is None


def test_spread_falls_back_to_settle_when_a_leg_is_thin():
    r = spread_m1m2(legs("近月", "SN2702", 407700, 405410, 792),
                    legs("次月", "SN2703", 407570, 405980, 297), 500)
    assert (r.status, r.value) == (OK, 405410 - 405980)
    assert r.params["price_field"] == "settle"
    assert "SN2703 成交 297 手" in r.note and "两端均采用结算价" in r.note


def test_spread_never_mixes_price_types():
    near = leg("近月", "SN2610", "close", 407490)
    nxt = leg("次月", "SN2611", "settle", 405660)
    with pytest.raises(Violation) as e:
        same_caliber("price_type", "价格类型")([near, nxt])
    assert e.value.status == BLOCKED_CALIBER


# ---------- 仓单日变动 ----------

def test_stock_chg_ok_when_adjacent():
    r = stock_chg_d(warrant(4979, 18), warrant(5221, 17), adjacent=True)
    assert (r.status, r.value) == (OK, -242)  # 与交易所公布的“增减 -242”一致


@pytest.mark.parametrize("adjacent, reason", [(False, "中间有交易日缺数"), (None, "交易日历缺失")])
def test_stock_chg_blocks_when_not_provably_adjacent(adjacent, reason):
    r = stock_chg_d(warrant(4979, 18), warrant(5500, 16), adjacent=adjacent)
    assert r.status == BLOCKED_TIME and r.value is None and reason in r.note


def test_stock_chg_missing_previous():
    r = stock_chg_d(warrant(4979, 18), None, adjacent=None)
    assert r.status == MISSING and r.value is None


# ---------- 不变量：任何非 ok 结果都不得带数 ----------

@pytest.mark.parametrize("result", [
    basis(spot(when=at(17, 11)), fut()), basis(spot(when=at(18, 8)), fut()),
    basis(spot(), fut(kind="主力连续")), basis(None, None),
    stock_chg_d(warrant(1, 18), warrant(2, 16), adjacent=False), stock_chg_d(None, None, adjacent=None),
])
def test_invariant_no_value_unless_ok(result):
    assert result.status != OK and result.value is None and result.as_of is None
