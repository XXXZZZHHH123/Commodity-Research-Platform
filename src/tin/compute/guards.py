"""派生计算的同时点 / 同口径守卫（FR-5.2）。守卫不通过就不出数——宁可空，不可错。"""

from dataclasses import dataclass
from datetime import date, datetime

from tin.config import SHANGHAI

OK = "ok"
BLOCKED_TIME = "blocked_timestamp_mismatch"
BLOCKED_CALIBER = "blocked_caliber_mismatch"
MISSING = "missing_input"


@dataclass(frozen=True)
class Point:
    role: str
    series_id: str
    observation_id: int | None
    value: float
    as_of: datetime
    caliber: dict
    source: str

    @property
    def trade_date(self) -> date:
        return self.as_of.astimezone(SHANGHAI).date()

    def describe(self) -> str:
        cal = " ".join(str(self.caliber[k]) for k in ("contract", "price_type") if self.caliber.get(k))
        t = self.as_of.astimezone(SHANGHAI).strftime("%m-%d %H:%M")
        return f"{self.role}（{self.series_id}{'，' + cal if cal else ''}，{t}）"

    def to_input(self) -> dict:
        return {
            "role": self.role, "series_id": self.series_id, "observation_id": self.observation_id,
            "value": self.value, "as_of": self.as_of.astimezone(SHANGHAI).isoformat(), "source": self.source,
            "price_type": self.caliber.get("price_type"), "contract": self.caliber.get("contract"),
        }


class Violation(Exception):
    def __init__(self, status: str, note: str):
        super().__init__(note)
        self.status = status
        self.note = note


def same_trade_day(points: list[Point]) -> None:
    days = {p.trade_date for p in points}
    if len(days) > 1:
        detail = "；".join(p.describe() for p in points)
        raise Violation(BLOCKED_TIME, f"输入不在同一交易日：{detail}")


def max_gap_hours(hours: float):
    def check(points: list[Point]) -> None:
        stamps = [p.as_of for p in points]
        gap = (max(stamps) - min(stamps)).total_seconds() / 3600
        if gap > hours:
            detail = "；".join(p.describe() for p in points)
            raise Violation(BLOCKED_TIME, f"两端时点相差 {gap:.1f} 小时，超过容差 {hours:g} 小时：{detail}")
    return check


def same_caliber(key: str, label: str):
    def check(points: list[Point]) -> None:
        values = {p.caliber.get(key) for p in points}
        if len(values) > 1:
            detail = "；".join(f"{p.role}={p.caliber.get(key) or '未标注'}" for p in points)
            raise Violation(BLOCKED_CALIBER, f"{label}不一致：{detail}")
    return check


def contract_kind_in(*allowed: str):
    def check(points: list[Point]) -> None:
        for p in points:
            kind = p.caliber.get("contract_kind")
            if kind is not None and kind not in allowed:
                raise Violation(BLOCKED_CALIBER,
                                f"{p.role} 为「{kind}」，本公式只接受 {'/'.join(allowed)}（主力连续换月会跳价，不可混算）")
    return check
