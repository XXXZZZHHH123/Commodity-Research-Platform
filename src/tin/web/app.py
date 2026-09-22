import csv
import io
import json
from datetime import date, datetime, time
from pathlib import Path
from urllib.parse import quote, urlencode

from fastapi import BackgroundTasks, Body, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import ValidationError
from sqlalchemy import select, text

from tin.caliber.dictionary import DIMENSIONS
from tin.compute.engine import compute_day, latest_trade_date
from tin.config import SHANGHAI, settings
from tin.db import SessionLocal
from tin.export.board import (
    PER_CONTRACT,
    archive_days,
    audit_entries,
    contract_curve,
    core_cards,
    gaps,
    judgment_block,
    macro_featured,
    macro_groups,
    snapshot,
    source_catalog,
    threshold_radar,
    ticker,
)
from tin.export import templates as export_templates
from tin.export.excel import Column, build_export, build_import_template, field_catalog
from tin.ingest import excel_importer, import_jobs, vendor_terminal
from tin.ingest.record import RecordError, record
from tin.judgments.importer import import_text
from tin.judgments.service import JudgmentError, current, save_version, to_payload
from tin.models import AuditLog, Indicator, Judgment, TradingDay
from tin.schemas.caliber import Caliber
from tin.schemas.observation import ObservationIn

HERE = Path(__file__).parent
app = FastAPI(title="大宗商品研究工作台")
app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
templates = Jinja2Templates(directory=HERE / "templates")

V = settings.variety
# 指标未登记默认发布时刻时的兜底值（收盘截点）
FALLBACK_ENTRY_TIME = time(15, 0)


def _num(value, unit: str = "", signed: bool = False) -> str:
    if value is None:
        return "—"
    if unit == "元":
        digits = 4
    elif unit == "%":
        digits = 2
    elif unit in ("元/吨", "吨", "手", "实物吨") or float(value).is_integer():
        digits = 0
    else:
        digits = 2
    s = f"{value:+,.{digits}f}" if signed else f"{value:,.{digits}f}"
    return s.replace("-", "−")


def static_version() -> str:
    """静态资源指纹。页面每次渲染都带上它，避免浏览器拿着旧的 app.js 配新的 HTML——
    那样按钮在、函数不在，点了没反应且毫无提示。"""
    newest = max((f.stat().st_mtime for f in (HERE / "static").glob("*")), default=0)
    return str(int(newest))


templates.env.globals["static_version"] = static_version
templates.env.filters["num"] = _num
templates.env.filters["local"] = lambda dt: dt.astimezone(SHANGHAI).strftime("%Y-%m-%d %H:%M") if dt else ""


def _redirect(path: str, **params) -> RedirectResponse:
    q = urlencode({k: str(v) for k, v in params.items() if v is not None})
    return RedirectResponse(f"{path}?{q}" if q else path, status_code=303)


def _board_date(s, q: str | None) -> date:
    if q:
        try:
            requested = date.fromisoformat(q)
        except ValueError:
            raise HTTPException(400, "日期格式应为 YYYY-MM-DD") from None
        matched = s.scalar(
            select(TradingDay.trade_date)
            .where(TradingDay.trade_date <= requested.isoformat())
            .order_by(TradingDay.trade_date.desc())
            .limit(1)
        )
        if matched is None:
            raise HTTPException(404, "所选日期之前没有可用交易日")
        return date.fromisoformat(matched)
    d = latest_trade_date(s)
    if d is None:
        raise HTTPException(503, "尚无行情数据：请先运行 python -m tin.jobs daily")
    return d


def _neighbour_dates(s, d: date) -> tuple[str | None, str | None]:
    prev = s.scalar(select(TradingDay.trade_date).where(TradingDay.trade_date < d.isoformat())
                    .order_by(TradingDay.trade_date.desc()).limit(1))
    nxt = s.scalar(select(TradingDay.trade_date).where(TradingDay.trade_date > d.isoformat())
                   .order_by(TradingDay.trade_date).limit(1))
    return prev, nxt


def _manual_indicators(s) -> list[dict]:
    rows = s.scalars(select(Indicator).where(Indicator.fetch_mode == "manual", Indicator.status == "可用")
                     .order_by(Indicator.category, Indicator.series_id)).all()
    today = datetime.now(SHANGHAI).date()
    return [{"series_id": i.series_id, "name": i.name, "unit": i.unit, "frequency": i.frequency,
             "default_as_of": datetime.combine(today, i.default_entry_time or FALLBACK_ENTRY_TIME)
             .strftime("%Y-%m-%dT%H:%M")} for i in rows]


def _shell(s, nav: str, d: date | None = None, **extra) -> dict:
    """所有页面共用的外壳数据：行情条、日期切换、快速录入抽屉。"""
    ctx = {"nav": nav, "d": d.isoformat() if d else None, "manual_indicators": _manual_indicators(s)}
    if d is not None:
        ctx["ticker"] = ticker(s, V, d)
        ctx["prev_date"], ctx["next_date"] = _neighbour_dates(s, d)
        ctx["min_date"] = s.scalar(select(TradingDay.trade_date)
                                   .order_by(TradingDay.trade_date).limit(1))
        ctx["max_date"] = s.scalar(select(TradingDay.trade_date)
                                   .order_by(TradingDay.trade_date.desc()).limit(1))
    return {**ctx, **extra}


@app.get("/")
def root():
    return RedirectResponse(f"/{V.lower()}")


@app.get("/healthz", include_in_schema=False)
def healthz():
    """Deployment health check: the process and its configured database must both work."""
    with SessionLocal() as s:
        s.execute(text("SELECT 1"))
    return {"status": "ok"}


@app.get("/sn")
def variety_page(request: Request, date: str | None = None, err: str | None = None):
    with SessionLocal() as s:
        d = _board_date(s, date)
        today = datetime.now(SHANGHAI).date()
        return templates.TemplateResponse(request, "variety.html", _shell(
            s, "variety", d, cards=core_cards(s, V, d), judgment=judgment_block(s, V, today),
            radar=threshold_radar(s, V, d), gaps=gaps(s, V, d), curve=contract_curve(s, V, d),
            macro=macro_featured(s, d), liquidity_min=settings.liquidity_min_volume, err=err))


@app.get("/sn/macro")
def macro_page(request: Request, date: str | None = None):
    with SessionLocal() as s:
        d = _board_date(s, date)
        return templates.TemplateResponse(request, "macro.html", _shell(
            s, "macro", d, groups=macro_groups(s, d), sources=source_catalog()))


@app.get("/sn/judgment")
def judgment_page(request: Request, err: str | None = None):
    with SessionLocal() as s:
        d = latest_trade_date(s)
        today = datetime.now(SHANGHAI).date()
        versions = s.scalars(select(Judgment).where(Judgment.variety == V).order_by(Judgment.version.desc())).all()
        last_import = s.scalars(select(AuditLog).where(AuditLog.action == "判断导入")
                                .order_by(AuditLog.id.desc()).limit(1)).first()
        return templates.TemplateResponse(request, "judgment.html", _shell(
            s, "judgment", d, j=current(s, V), block=judgment_block(s, V, today), versions=versions,
            names={i.series_id: i.name for i in s.scalars(select(Indicator))},
            radar=threshold_radar(s, V, d) if d else [],
            report=last_import.detail if last_import else None, err=err))


@app.post("/sn/judgment/import")
async def judgment_import(file: UploadFile | None = None, text: str = Form(""), author: str = Form(...)):
    """上传接口：研究逻辑新版本（txt / md）→ 新草稿版本。"""
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


@app.get("/sn/entry")
def entry_form(request: Request, series_id: str | None = None, err: str | None = None):
    with SessionLocal() as s:
        return templates.TemplateResponse(request, "entry.html", _shell(
            s, "entry", latest_trade_date(s), indicators=_manual_indicators(s), selected=series_id, err=err,
            audit=audit_entries(s), now=datetime.now(SHANGHAI).strftime("%Y-%m-%dT%H:%M")))


@app.post("/sn/entry")
def entry_submit(series_id: str = Form(...), value: float = Form(...), as_of: str = Form(...),
                 entered_by: str = Form(...), note: str = Form(...), return_to: str = Form("/sn/entry")):
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
            return _redirect(return_to.split("?")[0], err=e)
        s.add(AuditLog(at=datetime.now(SHANGHAI), actor=entered_by.strip(), action="人工录入",
                       target_type="observation", target_id=series_id,
                       detail={"value": value, "as_of": when.isoformat(), "note": note}))
        s.commit()
        d = latest_trade_date(s)
        if d is not None:
            compute_day(s, d)
        text = f"已录入 {ind.name} = {value:g}，派生指标已重算" if row else "与已有值相同，未重复写入"
        base, _, query = return_to.partition("?")
        params = dict(p.split("=", 1) for p in query.split("&") if "=" in p)
        return _redirect(base, msg=text, **params)


@app.get("/sn/indicators")
def indicators_page(request: Request):
    with SessionLocal() as s:
        rows = [i for i in s.scalars(select(Indicator).order_by(Indicator.fetch_mode, Indicator.category,
                                                                Indicator.series_id))
                if not PER_CONTRACT.match(i.series_id)]
        return templates.TemplateResponse(request, "indicators.html", _shell(
            s, "indicators", latest_trade_date(s), rows=rows, dimensions=DIMENSIONS))


@app.get("/sn/reports")
def reports_page(request: Request, date: str | None = None):
    with SessionLocal() as s:
        files = archive_days(s, V)
        selected = next((f for f in files if f["date"] == date), files[0] if files else {"date": None})
        preview = ""
        if selected.get("date"):
            path = settings.exports_dir / V / f"{selected['date']}.json"
            if path.exists():
                snap = json.loads(path.read_text(encoding="utf-8"))
                preview = json.dumps(snap, ensure_ascii=False, indent=2)
        return templates.TemplateResponse(request, "reports.html", _shell(
            s, "reports", latest_trade_date(s), files=files, selected=selected, preview=preview))


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


@app.get("/sn/contracts.csv")
def contracts_csv(date: str | None = None):
    with SessionLocal() as s:
        d = _board_date(s, date)
        rows = contract_curve(s, V, d)["rows"]
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["交易日", "合约", "结算价", "收盘价", "成交量(手)", "持仓量(手)", "主力", "流动性"])
    for r in rows:
        writer.writerow([d.isoformat(), r["contract"], r["settle"], r["close"], r["volume"], r["oi"],
                         "是" if r["main"] else "", "活跃" if r["active"] else "低流动性"])
    buf.seek(0)
    return StreamingResponse(iter([buf.getvalue().encode("utf-8-sig")]), media_type="text/csv",
                             headers={"Content-Disposition": f'attachment; filename="{V}_contracts_{d}.csv"'})


# ---------- Excel 批量导入 ----------

XLSX_MEDIA = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


@app.get("/api/sn/import/template")
def import_template():
    with SessionLocal() as s:
        data = build_import_template(s, V)
    name = f"{V}_批量导入模板_{datetime.now(SHANGHAI):%Y%m%d}.xlsx"
    return StreamingResponse(iter([data]), media_type=XLSX_MEDIA,
                             headers={"Content-Disposition": f'attachment; filename="{quote(name)}"'})


@app.post("/api/sn/import/preview")
async def import_preview(file: UploadFile = File(...), actor: str = Form(...)):
    if not actor.strip():
        raise HTTPException(400, "请填写导入操作人")
    data = await file.read()
    name = file.filename or "未命名文件"

    # 数据商终端原样导出的工作簿动辄几十万条，逐行预览既装不下也没人看得完。
    # 它按供应商编码精确对齐，不存在"这列对应哪个指标"的不确定性，该确认的是批次级信息。
    if vendor_terminal.detect(data) is not None:
        with SessionLocal() as s:
            try:
                summary = vendor_terminal.summarize(s, io.BytesIO(data), V)
            except vendor_terminal.VendorFormatError as e:
                raise HTTPException(400, str(e)) from None
        return JSONResponse({**summary, "token": import_jobs.stage(data), "filename": name})

    with SessionLocal() as s:
        try:
            return JSONResponse(excel_importer.build_preview(s, data, name, actor.strip(), V))
        except excel_importer.ImportError_ as e:
            raise HTTPException(400, str(e)) from None


@app.post("/api/sn/import/terminal/commit")
def import_terminal_commit(tasks: BackgroundTasks, body: dict = Body(...)):
    """确认整体导入。立刻返回任务号，几分钟的入库放后台跑。"""
    actor = str(body.get("actor", "")).strip()
    if not actor:
        raise HTTPException(400, "请填写导入操作人")
    try:
        job_id = import_jobs.create_terminal_job(
            str(body.get("token", "")), actor, str(body.get("filename", "未命名文件")), V)
    except ValueError as e:
        raise HTTPException(400, str(e)) from None
    if not import_jobs.staging_path(str(body["token"])).exists():
        raise HTTPException(400, "暂存文件已过期，请重新上传")
    tasks.add_task(import_jobs.execute, job_id)
    return {"job_id": job_id}


@app.get("/api/sn/import/jobs/{job_id}")
def import_job_status(job_id: str):
    state = import_jobs.snapshot(job_id)
    if state is None:
        raise HTTPException(404, "任务不存在或已过期")
    return state


@app.post("/api/sn/import/commit")
def import_commit(body: dict = Body(...)):
    actor = str(body.get("actor", "")).strip()
    note = str(body.get("default_note", "")).strip()
    if not actor or not note:
        raise HTTPException(400, "导入操作人与来源说明均为必填")
    with SessionLocal() as s:
        try:
            result = excel_importer.commit_preview(
                s, str(body.get("preview_id", "")), list(body.get("selected_row_keys") or []),
                dict(body.get("column_overrides") or {}), actor, note)
        except excel_importer.ImportError_ as e:
            raise HTTPException(400, str(e)) from None
        s.add(AuditLog(at=datetime.now(SHANGHAI), actor=actor, action="Excel批量导入",
                       target_type="observation", target_id=str(body.get("preview_id", ""))[:80],
                       detail={"committed": result["committed"], "revisions": result["revisions"],
                               "stale": len(result["stale_rows"]), "note": note,
                               "recalculated_dates": result["recalculated_dates"]}))
        s.commit()
        return JSONResponse(result)


# ---------- Excel 自定义导出 ----------


@app.get("/api/sn/export/fields")
def export_fields():
    with SessionLocal() as s:
        return JSONResponse({"groups": field_catalog(s, V)})


@app.get("/api/sn/export/templates")
def export_templates_list():
    with SessionLocal() as s:
        return JSONResponse({"templates": export_templates.list_templates(s, V)})


@app.post("/api/sn/export/templates")
def export_templates_save(body: dict = Body(...)):
    with SessionLocal() as s:
        try:
            saved = export_templates.save_template(s, V, body)
        except export_templates.TemplateError as e:
            raise HTTPException(400, str(e)) from None
        return JSONResponse({"saved": saved, "templates": export_templates.list_templates(s, V)})


@app.delete("/api/sn/export/templates/{template_id}")
def export_templates_delete(template_id: int):
    with SessionLocal() as s:
        try:
            return JSONResponse(export_templates.delete_template(s, V, template_id))
        except export_templates.TemplateError as e:
            raise HTTPException(404, str(e)) from None


@app.post("/api/sn/export/excel")
def export_excel(body: dict = Body(...)):
    actor = str(body.get("actor", "")).strip()
    if not actor:
        raise HTTPException(400, "请填写导出人姓名（用于合规审计）")
    columns = [Column(field=str(c["field"]), label=str(c.get("label") or c["field"]), kind=str(c["kind"]))
               for c in (body.get("columns") or [])]
    if not columns:
        raise HTTPException(400, "请至少选择一个导出字段")

    with SessionLocal() as s:
        data, meta = build_export(
            s, V, columns, date_range=str(body.get("date_range") or "recent_30_trade_days"),
            start_date=body.get("start_date"), end_date=body.get("end_date"),
            sort_order=str(body.get("sort_order") or "desc"), actor=actor)
        # 商业授权数据带出系统必须留痕（04 §8）
        s.add(AuditLog(at=datetime.now(SHANGHAI), actor=actor, action="Excel数据导出",
                       target_type="export", target_id=f"{V}_{datetime.now(SHANGHAI):%Y%m%d%H%M%S}",
                       detail={"columns": [c.field for c in columns], "rows": meta["rows"],
                               "range": meta["range"], "skipped": meta["skipped"],
                               "commercial_sources": meta["commercial_sources"]}))
        s.commit()

    name = f"{V}_研究数据_{datetime.now(SHANGHAI):%Y%m%d}.xlsx"
    return StreamingResponse(iter([data]), media_type=XLSX_MEDIA, headers={
        "Content-Disposition": f'attachment; filename="{quote(name)}"',
        "X-Export-Rows": str(meta["rows"]),
        "X-Export-Skipped": quote(",".join(meta["skipped"])),
    })


@app.get("/api/sn/snapshot")
def api_snapshot(date: str | None = None):
    with SessionLocal() as s:
        return JSONResponse(snapshot(s, V, _board_date(s, date)))
