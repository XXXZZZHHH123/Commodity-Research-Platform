"""为某个交易日解析公式输入并落库派生值。取数统一为“截至当日 24:00 的最新值”，时点是否匹配交给守卫判定。"""

import re
from datetime import date, datetime, time, timedelta, timezone

from sqlalchemy import and_, func, select
from sqlalchemy.orm import Session

from tin.compute import formulas
from tin.compute.guards import Point
from tin.config import SHANGHAI, settings
from tin.models import Derived, Observation, TradingDay

CONTRACT_SETTLE = re.compile(r"^SHFE\.(?P<v>[A-Z]+)\.(?P<m>\d{4})\.settle$")


def day_end(d: date) -> datetime:
    return datetime.combine(d + timedelta(days=1), time(0), SHANGHAI)


def _to_point(o: Observation, role: str) -> Point:
    return Point(role, o.series_id, o.id, o.value, o.as_of, o.caliber_snapshot, o.source)


def latest_obs(session: Session, series_id: str, before: datetime) -> Observation | None:
    """截至 before 的最新一条观测（同一时点取最高修订号）。"""
    return session.scalars(
        select(Observation)
        .where(Observation.series_id == series_id, Observation.as_of < before)
        .order_by(Observation.as_of.desc(), Observation.revision.desc())
        .limit(1)
    ).first()


def latest_point(session: Session, series_id: str, role: str, d: date) -> Point | None:
    o = latest_obs(session, series_id, day_end(d))
    return _to_point(o, role) if o else None


def previous_point(session: Session, series_id: str, role: str, d: date) -> Point | None:
    o = latest_obs(session, series_id, datetime.combine(d, time(0), SHANGHAI))
    return _to_point(o, role) if o else None


def contracts_on(session: Session, variety: str, d: date) -> list[str]:
    start = datetime.combine(d, time(0), SHANGHAI)
    ids = session.scalars(
        select(Observation.series_id).distinct()
        .where(Observation.series_id.like(f"SHFE.{variety}.%.settle"),
               Observation.as_of >= start, Observation.as_of < day_end(d))
    )
    return sorted(m.group("m") for sid in ids if (m := CONTRACT_SETTLE.match(sid)))


def is_adjacent(session: Session, earlier: date, later: date) -> bool | None:
    a = session.get(TradingDay, earlier.isoformat())
    b = session.get(TradingDay, later.isoformat())
    if a is None or b is None:
        return None
    if a.year == b.year:
        return b.year_seq - a.year_seq == 1
    return b.year == a.year + 1 and b.year_seq == 1


def latest_trade_date(session: Session) -> date | None:
    v = session.scalar(select(func.max(TradingDay.trade_date)))
    return date.fromisoformat(v) if v else None


def _leg(session: Session, variety: str, month: str, role: str, d: date) -> dict[str, Point | None]:
    return {f: latest_point(session, f"SHFE.{variety}.{month}.{f}", role, d) for f in ("close", "settle", "volume")}


def compute_day(session: Session, d: date, variety: str | None = None) -> list[formulas.Result]:
    variety = variety or settings.variety
    results = [formulas.basis(
        latest_point(session, f"SMM.{variety}.spot.1", "现货", d),
        latest_point(session, f"SHFE.{variety}.main.settle", "期货", d),
    )]

    months = contracts_on(session, variety, d)
    if len(months) >= 2:
        results.append(formulas.spread_m1m2(_leg(session, variety, months[0], "近月", d),
                                            _leg(session, variety, months[1], "次月", d),
                                            settings.liquidity_min_volume))
    else:
        results.append(formulas.evaluate("SPREAD_M1M2", {"近月": None, "次月": None}, [], lambda p: 0.0))

    today = latest_point(session, f"SHFE.{variety}.warrant", "当日", d)
    if today is not None and today.trade_date != d:
        today = None
    prev = previous_point(session, f"SHFE.{variety}.warrant", "前一交易日", d)
    adjacent = is_adjacent(session, prev.trade_date, d) if (today and prev) else None
    results.append(formulas.stock_chg_d(today, prev, adjacent))

    now = datetime.now(timezone.utc)
    for r in results:
        session.add(Derived(formula_id=r.formula_id, variety=variety, trade_date=d.isoformat(), value=r.value,
                            as_of=r.as_of, inputs=r.inputs, params=r.params, status=r.status, note=r.note,
                            computed_at=now))
    session.commit()
    return results


def latest_derived(session: Session, variety: str, d: date) -> dict[str, Derived]:
    sub = (select(Derived.formula_id, func.max(Derived.id).label("id"))
           .where(Derived.variety == variety, Derived.trade_date == d.isoformat())
           .group_by(Derived.formula_id).subquery())
    rows = session.scalars(select(Derived).join(sub, and_(Derived.id == sub.c.id)))
    return {r.formula_id: r for r in rows}
