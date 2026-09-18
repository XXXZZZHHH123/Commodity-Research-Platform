"""一期派生指标（04 §1.11）。每个公式声明输入、守卫与容差；阻断时 value 必为 None。"""

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime

from tin.compute.guards import (
    BLOCKED_TIME,
    MISSING,
    OK,
    Point,
    Violation,
    contract_kind_in,
    max_gap_hours,
    same_caliber,
    same_trade_day,
)


@dataclass(frozen=True)
class FormulaSpec:
    formula_id: str
    name: str
    expression: str
    tolerance: str
    unit: str


REGISTRY: dict[str, FormulaSpec] = {s.formula_id: s for s in (
    FormulaSpec("BASIS", "基差", "现货价 − 主力合约结算价", "同一交易日，且两端时点差 ≤ 4 小时", "元/吨"),
    FormulaSpec("SPREAD_M1M2", "近月−次月价差", "近月合约价 − 次月合约价",
                "同一交易日、同一价格类型；任一端成交低于流动性阈值则两端均改用结算价", "元/吨"),
    FormulaSpec("STOCK_CHG_D", "仓单日变动", "当日注册仓单 − 前一交易日注册仓单",
                "两端必须为相邻交易日（按上期所交易日期号校验）", "吨"),
)}


@dataclass
class Result:
    formula_id: str
    value: float | None
    status: str
    note: str | None
    as_of: datetime | None
    inputs: list[dict]
    params: dict = field(default_factory=dict)


def evaluate(formula_id: str, points: dict[str, Point | None],
             checks: list[Callable[[list[Point]], None]],
             fn: Callable[[dict[str, Point]], float],
             params: dict | None = None, note: str | None = None) -> Result:
    present = [p for p in points.values() if p is not None]
    inputs = [p.to_input() for p in present]
    missing = [role for role, p in points.items() if p is None]
    if missing:
        return Result(formula_id, None, MISSING, f"缺少输入：{'、'.join(missing)}",
                      None, inputs, params or {})
    try:
        for check in checks:
            check(present)
    except Violation as v:
        return Result(formula_id, None, v.status, v.note, None, inputs, params or {})
    return Result(formula_id, fn(points), OK, note,
                  min(p.as_of for p in present), inputs, params or {})


def basis(spot: Point | None, future: Point | None) -> Result:
    return evaluate(
        "BASIS", {"现货": spot, "期货": future},
        [same_trade_day, max_gap_hours(4), contract_kind_in("具体合约", "主力合约")],
        lambda p: p["现货"].value - p["期货"].value,
    )


def spread_m1m2(near: dict[str, Point | None], nxt: dict[str, Point | None], min_volume: int) -> Result:
    """near / nxt 形如 {"close": Point, "settle": Point, "volume": Point}。"""
    vols = {"近月": near.get("volume"), "次月": nxt.get("volume")}
    thin = [f"{role} {v.caliber.get('contract', '')} 成交 {v.value:,.0f} 手"
            for role, v in vols.items() if v is not None and v.value < min_volume]
    unknown_volume = [role for role, v in vols.items() if v is None]
    use = "settle" if thin or unknown_volume else "close"
    note = None
    if thin:
        note = f"成交清淡（{'；'.join(thin)} < {min_volume} 手），两端均采用结算价"
    elif unknown_volume:
        note = f"{'、'.join(unknown_volume)}成交量缺失，无法判断流动性，两端均采用结算价"
    return evaluate(
        "SPREAD_M1M2", {"近月": near.get(use), "次月": nxt.get(use)},
        [same_trade_day, same_caliber("price_type", "价格类型"), contract_kind_in("具体合约")],
        lambda p: p["近月"].value - p["次月"].value,
        params={"price_field": use, "min_volume": min_volume}, note=note,
    )


def stock_chg_d(today: Point | None, prev: Point | None, adjacent: bool | None) -> Result:
    """adjacent 由调用方按交易日期号判定：True 相邻 / False 不相邻 / None 日历缺失无法确认。"""
    def check_adjacent(_points: list[Point]) -> None:
        if adjacent is True:
            return
        reason = "中间有交易日缺数" if adjacent is False else "交易日历缺失，无法确认相邻"
        raise Violation(BLOCKED_TIME,
                        f"{prev.describe()} 与 {today.describe()} 不能确认为相邻交易日（{reason}）")

    return evaluate(
        "STOCK_CHG_D", {"当日": today, "前一交易日": prev},
        [same_caliber("stock_scope", "库存范围"), check_adjacent],
        lambda p: p["当日"].value - p["前一交易日"].value,
    )
