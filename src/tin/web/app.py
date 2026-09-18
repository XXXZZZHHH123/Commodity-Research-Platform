from datetime import date, datetime
from pathlib import Path
from urllib.parse import urlencode

from fastapi import FastAPI, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import ValidationError
from sqlalchemy import select

from tin.caliber.dictionary import DIMENSIONS
from tin.compute.engine import compute_day, latest_trade_date
from tin.config import SHANGHAI, settings
from tin.db import SessionLocal
from tin.export.board import PER_CONTRACT, core_cards, gaps, judgment_block, snapshot
from tin.ingest.record import RecordError, record
from tin.judgments.importer import import_text
from tin.judgments.service import JudgmentError, current, save_version, to_payload
from tin.models import AuditLog, Indicator, Judgment
from tin.schemas.caliber import Caliber
from tin.schemas.observation import ObservationIn

HERE = Path(__file__).parent
app = FastAPI(title="大宗商品研究工作台")
app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
templates = Jinja2Templates(directory=HERE / "templates")

V = settings.variety


def _num(value, unit: str = "", signed: bool = False) -> str:
    if value is None:
        return "—"
    if unit == "元":
        digits = 4
    elif unit in ("元/吨", "吨", "手", "实物吨") or float(value).is_integer():
        digits = 0
    else:
        digits = 2
    s = f"{value:+,.{digits}f}" if signed else f"{value:,.{digits}f}"
    return s.replace("-", "−")


templates.env.filters["num"] = _num
templates.env.filters["local"] = lambda dt: dt.astimezone(SHANGHAI).strftime("%Y-%m-%d %H:%M") if dt else ""


def _redirect(path: str, **params) -> RedirectResponse:
    q = urlencode({k: str(v) for k, v in params.items() if v is not None})
    return RedirectResponse(f"{path}?{q}" if q else path, status_code=303)


def _board_date(s, q: str | None) -> date:
    if q:
        return date.fromisoformat(q)
    d = latest_trade_date(s)
    if d is None:
        raise HTTPException(503, "尚无行情数据：请先运行 python -m tin.jobs daily")
    return d


@app.get("/")
def root():
    return RedirectResponse(f"/{V.lower()}")


@app.get("/sn")
def variety_page(request: Request, date: str | None = None, msg: str | None = None):
    with SessionLocal() as s:
        d = _board_date(s, date)
        today = datetime.now(SHANGHAI).date()
        return templates.TemplateResponse(request, "variety.html", {
            "d": d, "cards": core_cards(s, V, d), "judgment": judgment_block(s, V, today),
            "gaps": gaps(s, V, d), "msg": msg, "nav": "variety",
        })


@app.get("/sn/entry")
def entry_form(request: Request, series_id: str | None = None, msg: str | None = None, err: str | None = None):
    with SessionLocal() as s:
        manual = s.scalars(select(Indicator).where(Indicator.fetch_mode == "manual", Indicator.status == "可用")
                           .order_by(Indicator.category, Indicator.series_id)).all()
        return templates.TemplateResponse(request, "entry.html", {
            "indicators": manual, "selected": series_id, "msg": msg, "err": err, "nav": "indicators",
            "now": datetime.now(SHANGHAI).strftime("%Y-%m-%dT%H:%M"),
        })


@app.post("/sn/entry")
def entry_submit(series_id: str = Form(...), value: float = Form(...), as_of: str = Form(...),
                 entered_by: str = Form(...), note: str = Form(...)):
    with SessionLocal() as s:
        ind = s.get(Indicator, series_id)
        if ind is None or ind.fetch_mode != "manual":
            raise HTTPException(400, "只能为人工录入指标录入数值")
        when = datetime.fromisoformat(as_of).replace(tzinfo=SHANGHAI)
        try:
            row = record(s, ObservationIn(series_id=series_id, value=value, as_of=when,
                                          caliber=Caliber.model_validate(ind.caliber), source="人工",
                                          entered_by=entered_by.strip(), note=note.strip()))
        except (RecordError, ValidationError) as e:
            return _redirect("/sn/entry", series_id=series_id, err=e)
        s.add(AuditLog(at=datetime.now(SHANGHAI), actor=entered_by.strip(), action="人工录入",
                       target_type="observation", target_id=series_id,
                       detail={"value": value, "as_of": when.isoformat(), "note": note}))
        s.commit()
        d = latest_trade_date(s)
        if d is not None:
            compute_day(s, d)
        text = f"已录入 {ind.name} = {value:g}" if row else "与已有值相同，未重复写入"
        return _redirect("/sn/entry", series_id=series_id, msg=text)


@app.get("/sn/judgment")
def judgment_page(request: Request, msg: str | None = None, err: str | None = None):
    with SessionLocal() as s:
        today = datetime.now(SHANGHAI).date()
        versions = s.scalars(select(Judgment).where(Judgment.variety == V).order_by(Judgment.version.desc())).all()
        j = current(s, V)
        names = {i.series_id: i.name for i in s.scalars(select(Indicator))}
        last_import = s.scalars(select(AuditLog).where(AuditLog.action == "判断导入")
                                .order_by(AuditLog.id.desc()).limit(1)).first()
        return templates.TemplateResponse(request, "judgment.html", {
            "j": j, "block": judgment_block(s, V, today), "versions": versions, "names": names,
            "msg": msg, "err": err, "report": last_import.detail if last_import else None, "nav": "judgment",
        })


@app.post("/sn/judgment/import")
async def judgment_import(file: UploadFile | None = None, text: str = Form(""), author: str = Form(...)):
    """预留的上传接口：研究逻辑新版本（txt / md）→ 新草稿版本。"""
    raw = (await file.read()).decode("utf-8") if file is not None and file.filename else text
    if not raw.strip():
        return _redirect("/sn/judgment", err="没有收到文本")
    with SessionLocal() as s:
        latest = current(s, V)
        codes = {i.vendor_code: i.series_id for i in s.scalars(select(Indicator)) if i.vendor_code}
        try:
            payload, rep = import_text(raw, variety=V, author=author.strip(),
                                       base=to_payload(latest) if latest else None, vendor_codes=codes)
            row = save_version(s, payload, actor=author.strip())
        except (JudgmentError, ValidationError) as e:
            return _redirect("/sn/judgment", err=e)
        s.add(AuditLog(at=datetime.now(SHANGHAI), actor=author.strip(), action="判断导入",
                       target_type="judgment", target_id=f"{V}v{row.version}",
                       detail={"version": row.version, "mapped": rep.mapped, "carried": rep.carried,
                               "unmatched": rep.unmatched}))
        s.commit()
        return _redirect("/sn/judgment", msg=f"已导入为草稿 v{row.version}")


@app.get("/sn/indicators")
def indicators_page(request: Request):
    with SessionLocal() as s:
        rows = [i for i in s.scalars(select(Indicator).order_by(Indicator.fetch_mode, Indicator.category,
                                                                  Indicator.series_id))
                if not PER_CONTRACT.match(i.series_id)]
        return templates.TemplateResponse(request, "indicators.html", {
            "rows": rows, "dimensions": DIMENSIONS, "nav": "indicators"})


@app.get("/sn/reports")
def reports_page(request: Request):
    folder = settings.exports_dir / V
    files = sorted(folder.glob("*.json"), reverse=True) if folder.exists() else []
    return templates.TemplateResponse(request, "reports.html", {
        "files": [f.stem for f in files], "nav": "reports"})


@app.get("/sn/reports/{day}.json")
def report_file(day: str):
    try:
        date.fromisoformat(day)
    except ValueError:
        raise HTTPException(400, "日期格式应为 YYYY-MM-DD") from None
    path = settings.exports_dir / V / f"{day}.json"
    if not path.exists():
        raise HTTPException(404, "该日没有归档快照")
    return FileResponse(path, media_type="application/json", filename=f"{V}_{day}.json")


@app.get("/api/sn/snapshot")
def api_snapshot(date: str | None = None):
    with SessionLocal() as s:
        return JSONResponse(snapshot(s, V, _board_date(s, date)))
