"""统一指标目录、自选预览和共同日期上的走势对比；只读现有观测。"""

from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from tin.config import SHANGHAI
from tin.export.board import PER_CONTRACT, _chart_points, _series_history, expected_date, macro_history
from tin.ingest.fred import FRED_SERIES, SERIES_BY_ID
from tin.models import Indicator


def catalog(session: Session) -> list[dict]:
    rows = {}
    for ind in session.scalars(select(Indicator).order_by(Indicator.category, Indicator.series_id)):
        if PER_CONTRACT.match(ind.series_id):
            continue
        rows[ind.series_id] = {key: getattr(ind, key) for key in (
            "series_id", "name", "category", "caliber", "unit", "source", "frequency",
            "fetch_mode", "status", "vendor_code", "is_proxy", "note")}
        rows[ind.series_id]["registered"] = True
        rows[ind.series_id]["macro"] = False
    # 尚未采集的宏观序列也能被找到和自选，读页面不隐式登记或采集数据。
    for spec in FRED_SERIES:
        row = rows.setdefault(spec.series_id, {
            "series_id": spec.series_id, "caliber": {}, "status": "尚未采集",
            "vendor_code": spec.fred_id, "is_proxy": False, "note": "",
            "fetch_mode": "auto", "registered": False,
        })
        row.update(name=spec.name, category=spec.group, unit=spec.display_unit or spec.unit,
                   source=spec.provider, frequency=spec.frequency, macro=True)
    return sorted(rows.values(), key=lambda r: (r["macro"], r["category"], r["series_id"]))


def history(session: Session, series_id: str, through: date, limit: int = 900) -> dict | None:
    macro = macro_history(session, series_id, through, limit)
    if macro is not None:
        return macro
    ind = session.get(Indicator, series_id)
    if ind is None or PER_CONTRACT.match(series_id):
        return None
    rows = _series_history(session, series_id, through, limit)
    return {
        "series_id": series_id, "name": ind.name, "unit": ind.unit, "raw_unit": ind.unit,
        "frequency": ind.frequency, "provider": ind.source, "publisher": ind.source,
        "source_level": "代理" if ind.is_proxy else "登记指标", "decimals": 2,
        "change_unit": "百分点" if ind.unit == "%" else ind.unit,
        "through": through.isoformat(), "source_url": rows[-1].source_url if rows else None,
        "points": [{"date": r.as_of.astimezone(SHANGHAI).date().isoformat(), "value": r.value}
                   for r in rows],
    }


def previews(session: Session, series_ids: list[str], through: date) -> list[dict]:
    cards = []
    for sid in dict.fromkeys(series_ids):
        data = history(session, sid, through, 180)
        if data is None:
            raise ValueError(f"未知指标：{sid}")
        ind = session.get(Indicator, sid)
        spec = SERIES_BY_ID.get(sid)
        exp = expected_date(ind, through) if ind else None
        points = data.pop("points")
        values = [p["value"] for p in points]
        card = {**data, "status": "missing", "points": "", "stale": False,
                "retired": bool(ind and ind.status == "停用"), "registered": ind is not None,
                "note": "截至所选日期尚无数据"}
        if points:
            decimals = data["decimals"]
            card.update(status="ok", value=f"{values[-1]:,.{decimals}f}",
                        as_of=points[-1]["date"], count=len(points), points=_chart_points(values),
                        min=f"{min(values):,.{decimals}f}", max=f"{max(values):,.{decimals}f}",
                        stale=bool(exp and points[-1]["date"] < exp.isoformat()), note="")
            if len(points) > 1:
                delta = values[-1] - values[-2]
                card["change"] = (f"{delta * 100:+.0f} bp" if spec and spec.unit == "%"
                                  else f"{delta:+,.{decimals}f}" + (" 个百分点" if data["unit"] == "%" else ""))
        cards.append(card)
    return cards


def compare(session: Session, series_ids: list[str], through: date) -> dict:
    histories = [history(session, sid, through, 2000) for sid in series_ids]
    if any(h is None for h in histories):
        raise ValueError("包含未知指标")
    # 同日多个观测取最后一个；只对齐真实日期，低频数据不向前填充。
    by_date = [{p["date"]: p["value"] for p in h["points"]} for h in histories]
    dates = sorted(set.intersection(*(set(p) for p in by_date)))
    if len(dates) < 2:
        return {"status": "unavailable", "reason": "共同观测日期不足两个，无法比较走势。可检查数据时点与更新频率。"}
    if any(p[dates[0]] <= 0 for p in by_date):
        return {"status": "unavailable", "reason": "共同起点存在零值或负值，不适合以起点 100 归一化。请查看单指标原值。"}
    return {"status": "ok", "dates": dates, "through": through.isoformat(), "series": [
        {"series_id": h["series_id"], "name": h["name"], "unit": h["unit"],
         "raw_values": [p[d] for d in dates], "values": [p[d] / p[dates[0]] * 100 for d in dates]}
        for h, p in zip(histories, by_date)
    ]}
