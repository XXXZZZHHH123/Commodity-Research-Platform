"""品种页与每日 JSON 快照的共同数据源：页面和快照永远出自同一份组装结果。"""

import re
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from tin.compute.engine import day_end, latest_derived, latest_obs
from tin.compute.formulas import REGISTRY
from tin.config import SHANGHAI
from tin.judgments.service import current as current_judgment
from tin.judgments.service import to_payload
from tin.judgments.validate import activation_blockers
from tin.models import FetchRun, Indicator

PER_CONTRACT = re.compile(r"^SHFE\.[A-Z]+\.\d{4}\.")
OVERSEAS = {"CBOE", "LME", "代理指标"}
SOURCE_ABBR = {"SHFE": "上期所", "CFETS": "外汇交易中心", "CBOE": "CBOE", "SMM": "SMM", "Mysteel": "Mysteel"}


def _local(dt: datetime | None) -> str | None:
    return dt.astimezone(SHANGHAI).strftime("%Y-%m-%d %H:%M") if dt else None


def _iso(dt: datetime | None) -> str | None:
    return dt.astimezone(SHANGHAI).isoformat() if dt else None


def _prev_weekday(d: date) -> date:
    d -= timedelta(days=1)
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d


def expected_date(ind: Indicator, d: date) -> date | None:
    """在看板日 d，这个指标“至少应该”有哪一天的数据。None 表示不做缺口判定（事件驱动）。"""
    if ind.frequency == "日":
        return _prev_weekday(d) if ind.source in OVERSEAS else d
    if ind.frequency == "周":
        return d - timedelta(days=7)
    if ind.frequency == "月":
        return d - timedelta(days=62)
    return None


@dataclass
class Card:
    key: str
    label: str
    unit: str
    status: str  # ok / missing / blocked
    value: float | None = None
    as_of: str | None = None
    source: str | None = None
    caliber: str | None = None
    manual: bool = False
    proxy: bool = False
    entered: str | None = None
    expected_by: str | None = None
    note: str | None = None
    sub: str | None = None
    detail: dict = field(default_factory=dict)


def _obs_card(session: Session, key: str, label: str, series_id: str, d: date) -> Card:
    ind = session.get(Indicator, series_id)
    o = latest_obs(session, series_id, day_end(d))
    exp = expected_date(ind, d)
    card = Card(key=key, label=label, unit=ind.unit, status="missing", manual=ind.fetch_mode == "manual",
                proxy=ind.is_proxy, source=SOURCE_ABBR.get(ind.source, ind.source),
                expected_by=exp.isoformat() if exp else None)
    if o is None:
        card.note = "尚无数据"
        return card
    if exp and o.as_of.astimezone(SHANGHAI).date() < exp:
        card.note = f"最近一次数据时点 {_local(o.as_of)}，已过预期更新日"
        return card
    cal = o.caliber_snapshot
    card.status, card.value, card.as_of = "ok", o.value, _local(o.as_of)
    card.caliber = " · ".join(str(cal[k]) for k in ("contract", "price_type", "stock_scope", "spot_source") if cal.get(k))
    if card.manual:
        card.entered = f"{o.entered_by} 录入于 {_local(o.fetched_at)}"
    card.detail = {"series_id": series_id, "source_url": o.source_url, "caliber": cal,
                   "revision": o.revision, "note": o.note}
    return card


def _derived_card(derived: dict, key: str, label: str, formula_id: str) -> Card:
    spec = REGISTRY[formula_id]
    r = derived.get(formula_id)
    if r is None:
        return Card(key=key, label=label, unit=spec.unit, status="missing", note="当日尚未计算")
    detail = {"formula": spec.expression, "tolerance": spec.tolerance, "inputs": r.inputs, "params": r.params}
    if r.status != "ok":
        status = "missing" if r.status == "missing_input" else "blocked"
        return Card(key=key, label=label, unit=spec.unit, status=status, note=r.note, detail=detail)
    return Card(key=key, label=label, unit=spec.unit, status="ok", value=r.value, as_of=_local(r.as_of),
                source="派生", note=r.note, detail=detail)


def core_cards(session: Session, variety: str, d: date) -> list[Card]:
    derived = latest_derived(session, variety, d)
    main = _obs_card(session, "main", "主力合约结算价", f"SHFE.{variety}.main.settle", d)
    warrant = _obs_card(session, "warrant", "上期所注册仓单", f"SHFE.{variety}.warrant", d)
    chg = _derived_card(derived, "warrant_chg", "仓单日变动", "STOCK_CHG_D")
    if chg.status == "ok":
        warrant.sub = f"日变动 {chg.value:+,.0f} 吨".replace("-", "−")
    else:
        warrant.sub = "日变动：" + ("口径不一致，故不计算" if chg.status == "blocked" else "暂无")
    warrant.detail = {**warrant.detail, "derived": asdict(chg)}
    return [
        main,
        _obs_card(session, "spot", "现货价（SMM 1#）", f"SMM.{variety}.spot.1", d),
        _derived_card(derived, "basis", "基差", "BASIS"),
        _derived_card(derived, "spread", "近月−次月价差", "SPREAD_M1M2"),
        warrant,
        _obs_card(session, "fx", "美元兑人民币中间价", "FX.USDCNY.mid", d),
    ]


def _fetcher_of(series_id: str) -> str | None:
    for suffix, name in ((".main.", "shfe_quotes"), (".warrant", "shfe_warrant"), (".stock.weekly", "shfe_weekly_stock")):
        if series_id.startswith("SHFE.") and suffix in series_id:
            return name
    return {"FX.USDCNY.mid": "cfets_fx", "MACRO.VIX": "cboe_vix"}.get(series_id)


def _auto_gap_reason(session: Session, series_id: str, d: date) -> str:
    run = session.scalars(select(FetchRun)
                          .where(FetchRun.fetcher == _fetcher_of(series_id), FetchRun.target_date == d.isoformat())
                          .order_by(FetchRun.id.desc()).limit(1)).first()
    if run is None:
        return "当日未执行取数"
    if run.status == "失败":
        return f"自动取数失败（重试 {run.attempts} 次）：{run.error}"
    if run.status == "未发布":
        return f"数据源尚未发布：{run.error}"
    return "自动取数未取到"


def gaps(session: Session, variety: str, d: date) -> list[dict]:
    out = []
    for ind in session.scalars(select(Indicator).where(Indicator.status == "可用",
                                                       Indicator.variety.in_([variety, "COMMON"]))):
        if PER_CONTRACT.match(ind.series_id):
            continue
        exp = expected_date(ind, d)
        if exp is None:
            continue
        o = latest_obs(session, ind.series_id, day_end(d))
        last = o.as_of.astimezone(SHANGHAI).date() if o else None
        if last is not None and last >= exp:
            continue
        reason = "人工录入未更新" if ind.fetch_mode == "manual" else _auto_gap_reason(session, ind.series_id, d)
        out.append({"series_id": ind.series_id, "name": ind.name, "fetch_mode": ind.fetch_mode,
                    "expected_by": exp.isoformat(), "last_as_of": last.isoformat() if last else None,
                    "reason": reason if o else f"{reason}（尚无任何数据）"})
    return sorted(out, key=lambda g: (g["fetch_mode"] != "auto", g["series_id"]))


def judgment_block(session: Session, variety: str, today: date) -> dict | None:
    j = current_judgment(session, variety)
    if j is None:
        return None
    expired = (today - j.review_due).days
    return {
        "id": j.id, "version": j.version, "status": j.status, "author": j.author,
        "written_at": j.written_at.isoformat(), "review_due": j.review_due.isoformat(),
        "expired_days": expired if expired > 0 else 0, "tone": j.tone, "tone_note": j.tone_note,
        "contradiction": j.contradiction, "priority_rule": j.priority_rule,
        "thresholds": j.thresholds, "falsifiers": j.falsifiers, "unstructured": j.unstructured,
        "blockers": activation_blockers(to_payload(j)),
    }


def snapshot(session: Session, variety: str, d: date) -> dict:
    """04 §1.9 的每日 JSON 快照结构。signals 于 M3 监测层接入前为空列表。"""
    obs = []
    for ind in session.scalars(select(Indicator).where(Indicator.variety.in_([variety, "COMMON"]))):
        o = latest_obs(session, ind.series_id, day_end(d))
        if o is None or (PER_CONTRACT.match(ind.series_id) and o.as_of.astimezone(SHANGHAI).date() != d):
            continue
        obs.append({"series_id": o.series_id, "value": o.value, "unit": ind.unit, "as_of": _iso(o.as_of),
                    "caliber": o.caliber_snapshot, "source": o.source, "source_url": o.source_url,
                    "fetch_mode": ind.fetch_mode, "revision": o.revision})
    derived = [{"formula_id": r.formula_id, "value": r.value, "status": r.status, "note": r.note,
                "as_of": _iso(r.as_of), "inputs": r.inputs, "params": r.params}
               for r in latest_derived(session, variety, d).values()]
    today = datetime.now(SHANGHAI).date()
    return {
        "variety": variety, "as_of": d.isoformat(),
        "generated_at": datetime.now(timezone.utc).astimezone(SHANGHAI).isoformat(timespec="seconds"),
        "judgment": judgment_block(session, variety, today),
        "observations": sorted(obs, key=lambda x: x["series_id"]),
        "derived": sorted(derived, key=lambda x: x["formula_id"]),
        "signals": [], "gaps": gaps(session, variety, d), "surveys": [],
    }
