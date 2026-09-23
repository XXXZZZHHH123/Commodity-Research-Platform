"""Strategy lifecycle, evidence gates, and fill-based accounting.

Prices are CNY/tonne and the SHFE tin multiplier is one tonne per lot. This
module never treats a quote touching a level as an execution.
"""

from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from tin.models import Indicator, Observation, Strategy, StrategyEvent
from tin.schemas.strategy import ExecutionIn, StrategyPlan


class StrategyError(ValueError):
    pass


def _now():
    return datetime.now(timezone.utc)


def list_strategies(session: Session):
    return list(session.scalars(select(Strategy).order_by(Strategy.created_at.desc(), Strategy.id.desc())))


def get_strategy(session: Session, strategy_id: int):
    return session.get(Strategy, strategy_id)


def _event(session, row, event_type, actor, reason, payload=None):
    session.add(StrategyEvent(strategy_id=row.id, event_type=event_type, actor=actor,
                              reason=reason, at=_now(), payload=payload or {}))


def create_strategy(session: Session, plan: StrategyPlan, *, actor: str, reason: str,
                    revision_of: int | None = None):
    if not isinstance(plan, StrategyPlan):
        plan = StrategyPlan.model_validate(plan)
    row = Strategy(title=plan.title, variety=plan.variety, kind=plan.kind, mode=plan.mode,
                   status="draft", plan=plan.model_dump(mode="json"), author=actor,
                   revision_of=revision_of, version=1, created_at=_now(),
                   entry_fills=[], exit_fills=[])
    session.add(row)
    session.flush()
    _event(session, row, "created", actor, reason, {"plan": row.plan})
    return row


def _plan(row):
    return StrategyPlan.model_validate(row.plan)


def _quote_snapshot(session: Session, plan: StrategyPlan, now: datetime):
    """Verify exact contract quote registrations and persist the publication snapshot."""
    max_age = timedelta(hours=plan.max_quote_age_hours)
    quotes = []
    for leg in plan.legs:
        indicator = session.get(Indicator, leg.quote_series_id)
        if indicator is None or indicator.variety != plan.variety:
            raise StrategyError(f"{leg.contract} 的具体合约行情指标未登记")
        obs = session.scalars(select(Observation).where(
            Observation.series_id == leg.quote_series_id,
            Observation.status == "正常", Observation.as_of <= now,
        ).order_by(Observation.as_of.desc(), Observation.revision.desc()).limit(1)).first()
        if obs is None:
            raise StrategyError(f"{leg.contract} 暂无可用报价")
        if obs.value <= 0 or now - obs.as_of > max_age:
            raise StrategyError(f"{leg.contract} 报价缺失、非正数或已过期")
        quotes.append({"contract": leg.contract, "series_id": obs.series_id,
                       "observation_id": obs.id, "value": obs.value,
                       "as_of": obs.as_of.isoformat(), "fetched_at": obs.fetched_at.isoformat(),
                       "source": obs.source, "source_url": obs.source_url})
    times = [datetime.fromisoformat(item["as_of"]) for item in quotes]
    if len(times) == 2 and abs((times[0] - times[1]).total_seconds()) > plan.max_leg_skew_minutes * 60:
        raise StrategyError("跨期两腿行情时点错位，暂不能发布")
    return quotes


def _validate_evidence(session: Session, plan: StrategyPlan):
    for evidence in plan.evidence:
        if evidence.observation_id is None:
            continue
        observation = session.get(Observation, evidence.observation_id)
        if observation is None:
            raise StrategyError(f"研究证据观测 #{evidence.observation_id} 不存在")
        if (observation.series_id != evidence.series_id or observation.value != evidence.value
                or observation.as_of != evidence.as_of or observation.source != evidence.source):
            raise StrategyError(f"研究证据观测 #{evidence.observation_id} 与原始数据不一致")
        if observation.status != "正常":
            raise StrategyError(f"研究证据观测 #{evidence.observation_id} 已失效")


def publish_strategy(session: Session, strategy_id: int, *, actor: str, reason: str = ""):
    row = get_strategy(session, strategy_id)
    if row is None:
        raise StrategyError("策略不存在")
    if row.status != "draft":
        raise StrategyError("只有草稿可以发布")
    now = _now()
    plan = _plan(row)
    _validate_evidence(session, plan)
    if plan.as_of > now:
        raise StrategyError("策略研究时点不能晚于当前时间")
    if plan.review_at <= now:
        raise StrategyError("复核时间已过，请更新策略后再发布")
    quotes = _quote_snapshot(session, plan, now)
    row.status = "watching"
    row.published_at = now
    _event(session, row, "published", actor, reason or "研究员审阅并发布", {"quotes": quotes})
    session.flush()
    return row


def _execution(plan: StrategyPlan, execution: ExecutionIn, *, existing=None, opening=False):
    legs = {leg.contract: leg for leg in plan.legs}
    if len(execution.fills) != len(plan.legs):
        raise StrategyError("必须完整记录策略全部腿的成交，不支持单腿裸露")
    contracts = [fill.contract for fill in execution.fills]
    if len(set(contracts)) != len(contracts) or set(contracts) != set(legs):
        raise StrategyError("成交合约必须与策略腿一一对应")
    now = _now()
    by_contract = {item["contract"]: item for item in existing or []}
    result = []
    units = []
    for fill in execution.fills:
        if fill.filled_at > now:
            raise StrategyError("成交时点不能晚于当前时间")
        if opening:
            if fill.filled_at < plan.as_of:
                raise StrategyError("开仓成交不能早于策略研究时点")
        else:
            prior = by_contract[fill.contract]
            if fill.filled_at < datetime.fromisoformat(prior["filled_at"]):
                raise StrategyError("平仓成交不能早于对应开仓成交")
            if fill.quantity != prior["quantity"]:
                raise StrategyError("首版策略要求逐腿全量平仓，不支持部分平仓")
        result.append(fill.model_dump(mode="json"))
        units.append(fill.quantity / legs[fill.contract].ratio)
    if max(units) - min(units) > 1e-9:
        raise StrategyError("策略各腿手数必须符合设定比例")
    if opening:
        metric = _metric(plan, {x.contract: x.price for x in execution.fills})
        if not plan.entry.low <= metric <= plan.entry.high:
            raise StrategyError("实际组合成交价不在策略入场区间内")
    return result


def _metric(plan, prices):
    if plan.kind == "outright":
        return prices[plan.legs[0].contract]
    return prices[plan.legs[0].contract] - prices[plan.legs[1].contract]


def open_strategy(session: Session, strategy_id: int, execution: ExecutionIn, *, actor: str):
    row = get_strategy(session, strategy_id)
    if row is None or row.status != "watching":
        raise StrategyError("只有等待入场的已发布策略可以登记成交")
    now = _now()
    plan = _plan(row)
    if row.published_at is None or now < row.published_at:
        raise StrategyError("开仓成交时点不能早于策略发布日期")
    if now > plan.review_at:
        raise StrategyError("策略已到复核时间，需先重新审阅后再开仓")
    row.entry_fills = _execution(plan, execution, opening=True)
    if any(datetime.fromisoformat(x["filled_at"]) < row.published_at for x in row.entry_fills):
        raise StrategyError("开仓成交不能早于策略发布日期")
    row.status = "open"
    row.opened_at = max(datetime.fromisoformat(x["filled_at"]) for x in row.entry_fills)
    _event(session, row, "opened", actor, execution.reason, {"fills": row.entry_fills})
    session.flush()
    return row


def close_strategy(session: Session, strategy_id: int, execution: ExecutionIn, *, actor: str):
    row = get_strategy(session, strategy_id)
    if row is None or row.status != "open":
        raise StrategyError("只有跟踪中的策略可以登记退出成交")
    row.exit_fills = _execution(_plan(row), execution, existing=row.entry_fills)
    if any(datetime.fromisoformat(x["filled_at"]) < row.opened_at for x in row.exit_fills):
        raise StrategyError("平仓成交不能早于策略开仓时点")
    row.status = "closed"
    row.closed_at = max(datetime.fromisoformat(x["filled_at"]) for x in row.exit_fills)
    _event(session, row, "closed", actor, execution.reason, {"fills": row.exit_fills})
    session.flush()
    return row


def cancel_strategy(session: Session, strategy_id: int, *, actor: str, reason: str = ""):
    row = get_strategy(session, strategy_id)
    if row is None or row.status not in ("draft", "watching"):
        raise StrategyError("只能作废尚未入场的策略")
    if not reason:
        raise StrategyError("作废必须说明原因")
    row.status = "cancelled"
    _event(session, row, "cancelled", actor, reason)
    session.flush()
    return row


def _realized(row):
    plan = _plan(row)
    entry = {x["contract"]: x for x in row.entry_fills}
    total = 0.0
    for exit_fill in row.exit_fills:
        original = entry[exit_fill["contract"]]
        side = next(x.side for x in plan.legs if x.contract == exit_fill["contract"])
        direction = 1 if side == "buy" else -1
        total += direction * (exit_fill["price"] - original["price"]) * exit_fill["quantity"]
        total -= original.get("costs", 0) + exit_fill.get("costs", 0)
    return round(total, 8)


def serialize_strategy(session: Session, row: Strategy):
    events = session.scalars(select(StrategyEvent).where(StrategyEvent.strategy_id == row.id)
                             .order_by(StrategyEvent.at, StrategyEvent.id)).all()
    result = {"id": row.id, "title": row.title, "variety": row.variety, "kind": row.kind,
              "mode": row.mode, "status": row.status, "plan": row.plan,
              "author": row.author, "revision_of": row.revision_of, "version": row.version,
              "created_at": row.created_at.isoformat(),
              "published_at": row.published_at.isoformat() if row.published_at else None,
              "opened_at": row.opened_at.isoformat() if row.opened_at else None,
              "closed_at": row.closed_at.isoformat() if row.closed_at else None,
              "entry_fills": row.entry_fills, "exit_fills": row.exit_fills,
              "events": [{"type": e.event_type, "actor": e.actor, "reason": e.reason,
                          "at": e.at.isoformat(), "payload": e.payload} for e in events]}
    if row.status == "closed":
        result["realized_pnl"] = _realized(row)
        result["realized_pnl_unit"] = "CNY, fees included"
    return result
