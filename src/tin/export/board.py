"""品种页与每日 JSON 快照的共同数据源：页面和快照永远出自同一份组装结果。"""

import re
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from tin.compute.engine import contracts_on, day_end, latest_derived, latest_obs
from tin.compute.formulas import REGISTRY
from tin.config import NEW_YORK, SHANGHAI, settings
from tin.ingest.fred import FEATURED_SERIES, FRED_SERIES, SOURCE_CATALOG, MacroSeries
from tin.judgments.service import current as current_judgment
from tin.judgments.service import to_payload
from tin.judgments.validate import activation_blockers
from tin.models import FetchRun, Indicator, Observation

PER_CONTRACT = re.compile(r"^SHFE\.[A-Z]+\.\d{4}\.")
OVERSEAS = {"CBOE", "LME", "代理指标", "FRED"}  # 境外源：北京时间当日尚未发布，按上一工作日要求
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
    series_id: str | None = None
    badge: str | None = None
    change: float | None = None
    change_pct: float | None = None
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


def previous_value(session: Session, series_id: str, before: datetime) -> float | None:
    """上一个数据点的值，用于算涨跌。取的是严格早于 before 的最近一条。"""
    o = latest_obs(session, series_id, before)
    return o.value if o else None


def _obs_card(session: Session, key: str, label: str, series_id: str, d: date) -> Card:
    ind = session.get(Indicator, series_id)
    o = latest_obs(session, series_id, day_end(d))
    exp = expected_date(ind, d)
    card = Card(key=key, label=label, unit=ind.unit, status="missing", manual=ind.fetch_mode == "manual",
                proxy=ind.is_proxy, source=SOURCE_ABBR.get(ind.source, ind.source),
                series_id=series_id, badge=ind.source,
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
    prev = previous_value(session, series_id, o.as_of)
    if prev is not None:
        card.change = o.value - prev
        card.change_pct = (card.change / prev * 100) if prev else None
    card.detail = {"series_id": series_id, "source_url": o.source_url, "caliber": cal,
                   "revision": o.revision, "note": o.note}
    return card


def _derived_card(derived: dict, key: str, label: str, formula_id: str, prev: float | None = None) -> Card:
    spec = REGISTRY[formula_id]
    r = derived.get(formula_id)
    if r is None:
        return Card(key=key, label=label, unit=spec.unit, status="missing", note="当日尚未计算", badge="派生")
    detail = {"formula": spec.expression, "tolerance": spec.tolerance, "inputs": r.inputs, "params": r.params}
    if r.status != "ok":
        status = "missing" if r.status == "missing_input" else "blocked"
        return Card(key=key, label=label, unit=spec.unit, status=status, note=r.note, detail=detail, badge="派生")
    card = Card(key=key, label=label, unit=spec.unit, status="ok", value=r.value, as_of=_local(r.as_of),
                source="派生", badge="派生", note=r.note, detail=detail)
    if prev is not None:
        card.change = r.value - prev
    return card


def previous_derived(session: Session, variety: str, d: date, formula_id: str) -> float | None:
    from tin.models import Derived
    row = session.scalars(
        select(Derived).where(Derived.variety == variety, Derived.formula_id == formula_id,
                              Derived.trade_date < d.isoformat(), Derived.value.is_not(None))
        .order_by(Derived.trade_date.desc(), Derived.id.desc()).limit(1)).first()
    return row.value if row else None


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
        _derived_card(derived, "basis", "基差", "BASIS", previous_derived(session, variety, d, "BASIS")),
        _derived_card(derived, "spread", "近月−次月价差", "SPREAD_M1M2",
                      previous_derived(session, variety, d, "SPREAD_M1M2")),
        warrant,
        _obs_card(session, "fx", "美元兑人民币中间价", "FX.USDCNY.mid", d),
    ]


def _series_history(session: Session, series_id: str, d: date, limit: int = 180) -> list[Observation]:
    """截至看板日的历史值；同一时点只保留最新修订。"""
    rows = session.scalars(
        select(Observation)
        .where(Observation.series_id == series_id, Observation.as_of < day_end(d))
        .order_by(Observation.as_of.desc(), Observation.revision.desc())
        .limit(limit * 3)
    ).all()
    unique: dict[datetime, Observation] = {}
    for row in rows:
        unique.setdefault(row.as_of, row)
    return sorted(unique.values(), key=lambda x: x.as_of)[-limit:]


def _chart_points(values: list[float], width: int = 100, height: int = 36) -> str:
    if not values:
        return ""
    low, high = min(values), max(values)
    span = high - low
    if len(values) == 1:
        return f"0,{height / 2:g} {width},{height / 2:g}"
    points = []
    for i, value in enumerate(values):
        x = i * width / (len(values) - 1)
        y = height / 2 if span == 0 else 3 + (high - value) * (height - 6) / span
        points.append(f"{x:.2f},{y:.2f}")
    return " ".join(points)


def _display_value(value: float, spec: MacroSeries) -> str:
    shown = value / spec.scale
    return f"{shown:,.{spec.decimals}f}"


def _source_date(row: Observation, spec: MacroSeries) -> date:
    zone = NEW_YORK if spec.market_close else timezone.utc
    return row.as_of.astimezone(zone).date()


def macro_cards(session: Session, d: date, series_ids: tuple[str, ...] | None = None) -> list[dict]:
    wanted = set(series_ids) if series_ids else None
    out = []
    for spec in FRED_SERIES:
        if wanted is not None and spec.series_id not in wanted:
            continue
        rows = _series_history(session, spec.series_id, d)
        values = [r.value / spec.scale for r in rows]
        item = {
            "series_id": spec.series_id, "name": spec.name, "group": spec.group,
            "unit": spec.display_unit or spec.unit, "provider": spec.provider,
            "source_level": spec.source_level, "publisher": spec.publisher,
            "frequency": spec.frequency, "status": "missing", "points": "",
        }
        if rows:
            latest = rows[-1]
            item.update({
                "status": "ok", "value": _display_value(latest.value, spec),
                "as_of": _source_date(latest, spec).isoformat(),
                "points": _chart_points(values), "count": len(rows),
                "min": f"{min(values):,.{spec.decimals}f}",
                "max": f"{max(values):,.{spec.decimals}f}",
                "source_url": latest.source_url,
            })
            if len(rows) >= 2:
                delta = values[-1] - values[-2]
                if spec.unit == "%":
                    item["change"] = f"{delta * 100:+.0f} bp"
                else:
                    item["change"] = f"{delta:+,.{spec.decimals}f}"
        out.append(item)
    return out


def macro_groups(session: Session, d: date) -> list[dict]:
    cards = macro_cards(session, d)
    return [{"name": group, "cards": [c for c in cards if c["group"] == group]}
            for group in dict.fromkeys(s.group for s in FRED_SERIES)]


def macro_featured(session: Session, d: date) -> list[dict]:
    return macro_cards(session, d, FEATURED_SERIES)


def source_catalog() -> dict:
    rows = [asdict(s) for s in SOURCE_CATALOG]
    counts = {status: sum(r["status"] == status for r in rows)
              for status in ("已接入", "待接入", "需凭证", "人工导入", "候选")}
    return {"rows": rows, "counts": counts}


def contract_curve(session: Session, variety: str, d: date) -> dict:
    main = latest_obs(session, f"SHFE.{variety}.main.settle", day_end(d))
    main_contract = (main.caliber_snapshot or {}).get("contract") if main else None
    rows = []
    for month in contracts_on(session, variety, d):
        values = {field: latest_obs(session, f"SHFE.{variety}.{month}.{field}", day_end(d))
                  for field in ("close", "settle", "volume", "oi")}
        settle = values["settle"]
        if settle is None or settle.as_of.astimezone(SHANGHAI).date() != d:
            continue
        contract = f"{variety}{month}"
        rows.append({
            "contract": contract, "month": month, "settle": settle.value,
            "close": values["close"].value if values["close"] else None,
            "volume": values["volume"].value if values["volume"] else None,
            "oi": values["oi"].value if values["oi"] else None,
            "main": contract == main_contract,
            "active": bool(values["volume"] and values["volume"].value >= settings.liquidity_min_volume),
        })
    prices = [r["settle"] for r in rows]
    active = [r for r in rows if r["active"]]
    shape = None
    if len(active) >= 2:
        shape = "Contango" if active[-1]["settle"] > active[0]["settle"] else "Backwardation"
    points = _chart_points(prices, 400, 120)
    for r, xy in zip(rows, points.split(), strict=False):
        x, y = xy.split(",")
        r["x"], r["y"] = float(x), float(y)
    return {
        "rows": rows, "points": points, "shape": shape,
        "min": min(prices) if prices else None, "max": max(prices) if prices else None,
        "mid": (min(prices) + max(prices)) / 2 if prices else None,
    }


def _fetcher_of(series_id: str) -> str | None:
    for suffix, name in ((".main.", "shfe_quotes"), (".warrant", "shfe_warrant"), (".stock.weekly", "shfe_weekly_stock")):
        if series_id.startswith("SHFE.") and suffix in series_id:
            return name
    if series_id.startswith("FRED."):
        return "fred_macro"
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
    if run.status == "部分失败":
        return f"宏观批次部分失败：{run.error}"
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


def _fmt(v: float | None, unit: str = "") -> str:
    if v is None:
        return "—"
    digits = 4 if unit == "元" else 0 if abs(v) >= 1000 or float(v).is_integer() else 2
    return f"{v:,.{digits}f}".replace("-", "−")


def threshold_radar(session: Session, variety: str, d: date) -> list[dict]:
    """阈值测距：只陈述当前值、触发条件与距离，不给「安全与否」的结论（P1）。"""
    j = current_judgment(session, variety)
    if j is None:
        return []
    out = []
    for t in j.thresholds:
        ind = session.get(Indicator, t["series_id"])
        o = latest_obs(session, t["series_id"], day_end(d)) if ind else None
        row = {"id": t["id"], "name": t["name"], "series_id": t["series_id"],
               "series_name": ind.name if ind else t["series_id"], "unit": ind.unit if ind else "",
               "type": t["type"], "horizon": t["horizon"], "action_hint": t.get("action_hint"),
               "note": t.get("note"), "state": "数据缺失", "value": None, "progress": 0,
               "distance": "该指标暂无数据", "as_of": None}
        op, target = t["operator"], t["value"]
        row["condition"] = (f"{_fmt(target[0])} – {_fmt(target[1])}" if op == "between"
                            else f"{op} {_fmt(target)}")
        if o is None:
            out.append(row)
            continue
        v = o.value
        row.update({"value": v, "as_of": _local(o.as_of)})
        if op == "between":
            lo, hi = target
            hit = lo <= v <= hi
            gap = 0 if hit else (lo - v if v < lo else v - hi)
            row["state"] = "在区间内" if hit else "未触发"
            row["progress"] = 100 if hit else max(0, 100 - abs(gap) / max(hi - lo, 1) * 100)
            row["distance"] = "已进入区间" if hit else f"距区间 {_fmt(abs(gap), row['unit'])} {row['unit']}"
        else:
            hit = (v > target if op == ">" else v >= target if op == ">=" else
                   v < target if op == "<" else v <= target)
            row["state"] = "已触发" if hit else "未触发"
            row["progress"] = min(100, v / target * 100 if target else 0) if op in (">", ">=") \
                else min(100, target / v * 100 if v else 0)
            row["distance"] = ("已越线" if hit else
                               f"距触发 {_fmt(abs(target - v), row['unit'])} {row['unit']}")
        out.append(row)
    return out


def ticker(session: Session, variety: str, d: date) -> list[dict]:
    """顶部行情条：核心数字的极简版，只显示已取到的。"""
    items = []
    for c in core_cards(session, variety, d):
        if c.status != "ok":
            continue
        items.append({"label": c.label.split("（")[0], "value": _fmt(c.value, c.unit), "unit": c.unit,
                      "change": c.change, "change_pct": c.change_pct})
    vix = _obs_card(session, "vix", "VIX", "MACRO.VIX", d)
    if vix.status == "ok":
        items.append({"label": "VIX", "value": _fmt(vix.value, vix.unit), "unit": "",
                      "change": vix.change, "change_pct": vix.change_pct})
    return items


def archive_days(session: Session, variety: str, limit: int = 40) -> list[dict]:
    """归档快照列表，摘要直接取自当日冻结的 JSON，不重算。"""
    import json
    folder = settings.exports_dir / variety
    if not folder.exists():
        return []
    out = []
    for path in sorted(folder.glob("*.json"), reverse=True)[:limit]:
        item = {"date": path.stem, "weekday": "一二三四五六日"[date.fromisoformat(path.stem).weekday()],
                "tone": None, "main": None, "gaps": None, "generated_at": None}
        try:
            snap = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            out.append(item)
            continue
        jm = snap.get("judgment") or {}
        main = next((o for o in snap.get("observations", [])
                     if o["series_id"] == f"SHFE.{variety}.main.settle"), None)
        item.update({"tone": jm.get("tone"), "gaps": len(snap.get("gaps", [])),
                     "generated_at": snap.get("generated_at"),
                     "main": _fmt(main["value"], main["unit"]) if main else None})
        out.append(item)
    return out


def audit_entries(session: Session, limit: int = 20) -> list[dict]:
    from tin.models import AuditLog
    rows = session.scalars(select(AuditLog).where(AuditLog.action == "人工录入")
                           .order_by(AuditLog.id.desc()).limit(limit)).all()
    names = {i.series_id: (i.name, i.unit) for i in session.scalars(select(Indicator))}
    out = []
    for r in rows:
        name, unit = names.get(r.target_id, (r.target_id, ""))
        detail = r.detail or {}
        as_of = detail.get("as_of")
        out.append({"as_of": as_of[:16].replace("T", " ") if as_of else "—", "name": name,
                    "series_id": r.target_id, "value": _fmt(detail.get("value"), unit), "unit": unit,
                    "actor": r.actor, "note": detail.get("note"), "at": _local(r.at)})
    return out
