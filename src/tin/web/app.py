import csv
import io
import json
import logging
from datetime import date, datetime, time
from pathlib import Path
from urllib.parse import quote, urlencode

from fastapi import BackgroundTasks, Body, FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import ValidationError
from sqlalchemy import select, text

from tin.brief import store as brief_store
from tin.caliber.dictionary import DIMENSIONS
from tin.compute.engine import compute_day, latest_trade_date
from tin.config import SHANGHAI, settings
from tin.db import SessionLocal
from tin import schema_check
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
    macro_history,
    snapshot,
    source_catalog,
    threshold_radar,
    ticker,
)
from tin.export import templates as export_templates
from tin.export.excel import Column, build_export, build_import_template, field_catalog
from tin.export import diagram as diagram_api
from tin.export import diagram_md
from tin.ingest import excel_importer, import_jobs, vendor_terminal
from tin.ingest.record import RecordError, record
from tin.jobs.seed import seed_researcher
from tin.judgments.importer import import_text
from tin.judgments.service import JudgmentError, current, save_version, to_payload
from tin.models import AuditLog, DailyBrief, Indicator, Judgment, TradingDay
from tin.schemas.brief import STANCES
from tin.schemas.caliber import Caliber
from tin.schemas.judgment import TONES
from tin.schemas.observation import ObservationIn
from tin.web import home
from tin.web.strategy import build_router as build_strategy_router

HERE = Path(__file__).parent
log = logging.getLogger("uvicorn.error")   # 借 uvicorn 的 logger：自建的默认没有 handler，写了也看不见
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


_migrate_error: str | None = None


@app.on_event("startup")
def _migrate_on_startup() -> None:
    """启动时自动把库升到最新。

    部署脚本和容器启动都没有迁移步骤，不自动跑就意味着每次带迁移的发布都得有人
    手动上服务器执行——漏跑的代价是整站 503，而漏跑几乎必然发生（本地已经连着
    绊了三次）。

    **前提是迁移前自动备份**，否则"自动改生产库"不该开（见 schema_check.backup）。
    **失败也不让进程退出**：起不来的服务只会留下一段终端里的堆栈；起得来、由 503
    页面把错误和手动命令一起显示出来，人才看得见。
    """
    global _migrate_error
    engine = schema_check.engine_of(SessionLocal)
    if engine is None or not settings.auto_migrate:
        return
    result = schema_check.auto_upgrade(engine, keep_backups=settings.migrate_backups)
    if not result.get("ran"):
        return
    if result["ok"]:
        # alembic 自己会逐条打出跑了哪些迁移，这里再给一句汇总，
        # 重点是**备份在哪**——真要回退的时候，得能一眼找到那个文件
        log.warning("数据库已自动升级到 %s%s", result.get("to"),
                    f"，迁移前备份：{result['backup']}" if result.get("backup")
                    else "（非 SQLite，未自动备份，请确认运维侧有备份策略）")
    else:
        _migrate_error = result["error"]
        log.error("数据库自动升级失败：%s", _migrate_error)


@app.middleware("http")
async def _guard_schema(request: Request, call_next):
    """库仍然落后时，在页面炸开之前先把话说清楚。

    正常情况下启动时已经自动升级过，这里不会触发。留着它是因为自动升级可能被关掉
    （`TIN_AUTO_MIGRATE=false`）、可能失败、也可能库是在进程起来之后才被换掉的。

    同一个坑绊了三次（`diagram_templates`、`researchers`…）：按表逐个 try/except
    补不完，每加一张表就多一个坑。这里只检查一次迁移版本，对不上就**所有页面**
    统一给出该敲的命令。
    """
    if not request.url.path.startswith("/static"):
        gap = schema_check.migration_gap(schema_check.engine_of(SessionLocal))
        if gap:
            if _migrate_error:
                gap = f"自动升级失败了：{_migrate_error}\n\n{gap}"
            if request.url.path.startswith("/api/"):
                return JSONResponse({"detail": gap}, status_code=503)
            return templates.TemplateResponse(
                request, "needs_migration.html",
                {"setup_error": gap, "project_root": str(schema_check.ROOT),
                 "auto_failed": bool(_migrate_error)},
                status_code=503)
    return await call_next(request)


templates.env.globals["static_version"] = static_version
templates.env.filters["num"] = _num
templates.env.filters["local"] = lambda dt: dt.astimezone(SHANGHAI).strftime("%Y-%m-%d %H:%M") if dt else ""

# Resolve SessionLocal at request time, preserving the app's test/deployment overrides.
app.include_router(build_strategy_router(lambda: SessionLocal(), templates,
                                         lambda *args, **kwargs: _shell(*args, **kwargs)))


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
    """所有页面共用的外壳数据：行情条、日期切换、快速录入抽屉。

    **行情条与日期选择器是两件事。** 行情条是"此刻的市场状态"，每一页都该有；
    日期选择器是页面自己的翻页，只有按日期取数的页面才需要。早先两者都挂在 `d` 上，
    于是不按日期取数的页面（策略页传 d=None）连行情条一起没了——那一条是研究员
    扫一眼就知道盘面的地方，缺了很显眼。
    """
    ctx = {"nav": nav, "d": d.isoformat() if d else None, "manual_indicators": _manual_indicators(s)}
    shown = d or latest_trade_date(s)
    if shown is not None:
        ctx["ticker"] = ticker(s, V, shown)
    if d is not None:
        ctx["prev_date"], ctx["next_date"] = _neighbour_dates(s, d)
        ctx["min_date"] = s.scalar(select(TradingDay.trade_date)
                                   .order_by(TradingDay.trade_date).limit(1))
        ctx["max_date"] = s.scalar(select(TradingDay.trade_date)
                                   .order_by(TradingDay.trade_date.desc()).limit(1))
    return {**ctx, **extra}


def _home_date(s, q: str | None) -> date | None:
    """首屏的日期比 `/sn` 宽容：没有行情数据也得出得来。

    `/sn` 是数据看板，没数据就该 503；首屏是「今天我该干什么」，待办与简报跟行情在不在
    没关系。整页 503 等于把人关在门外，而北极星恰恰是「我每天真的会打开它」。
    """
    if q:
        return _board_date(s, q)
    return latest_trade_date(s)


@app.get("/")
def home_page(request: Request, date: str | None = None, err: str | None = None):
    """首屏工作台（F9）：待办 → 今日简报 → 昨夜变化 → 全景数据墙（默认折叠）。"""
    with SessionLocal() as s:
        d = _home_date(s, date)
        researcher = seed_researcher(s)
        brief = brief_store.current(s, researcher.id, V, d) if d is not None else None
        return templates.TemplateResponse(request, "home.html", _shell(
            s, "home", d,
            researcher=researcher,
            todos=home.todos(s, V, d),
            brief=home.brief_view(s, brief, d) if brief is not None else None,
            brief_gap=None if brief is not None else home.brief_gap(s, researcher, d),
            changes=home.overnight(s, V, d) if d is not None else {"core": [], "macro": []},
            cards=core_cards(s, V, d) if d is not None else [],
            radar=threshold_radar(s, V, d) if d is not None else [],
            gaps=gaps(s, V, d) if d is not None else [],
            tones=TONES, stances=STANCES, editable=brief_store.EDITABLE_FIELDS, err=err))


@app.post("/api/sn/brief/{brief_id}/review")
def brief_review(brief_id: int, body: dict = Body(...)):
    """研究员的「采纳 / 改 / 否」。规则全在 `brief/store.review` 里，这里只负责转述。

    `store.BriefError` 一律转成 400 + 原文案：那些规则（改/否必须写原因、系统留痕不可
    改写）是写给研究员看的，包成「操作失败」或者漏成 500 就白写了。
    """
    action = str(body.get("action") or "").strip()
    reason = body.get("reason")
    reason = str(reason).strip() if reason is not None else None
    changes = body.get("changes") or None
    if changes is not None and not isinstance(changes, dict):
        raise HTTPException(400, "changes 必须是「字段名 → 新值」的对象")
    elapsed = body.get("elapsed_seconds")
    if elapsed is not None:
        try:
            elapsed = max(0, int(elapsed))
        except (TypeError, ValueError):
            raise HTTPException(400, "elapsed_seconds 必须是秒数（整数）") from None

    with SessionLocal() as s:
        actor = str(body.get("actor") or "").strip() or seed_researcher(s).display_name
        try:
            revisions = brief_store.review(s, brief_id, actor=actor, action=action, changes=changes,
                                           reason=reason, elapsed_seconds=elapsed)
        except brief_store.BriefError as e:
            raise HTTPException(400, str(e)) from None
        brief = s.get(DailyBrief, brief_id)
        return JSONResponse({"brief_id": brief_id, "status": brief.status, "actor": actor,
                             "revisions": len(revisions), "elapsed_seconds": elapsed,
                             "fields": [r.field for r in revisions if r.field]})


@app.post("/api/sn/brief/{brief_id}/reopen")
def brief_reopen(brief_id: int, body: dict = Body(...)):
    """把已处理的简报退回草稿，让重跑能再次生成。

    误点一次「否」不该让研究员当天再也拿不到简报——`save()` 拒绝覆盖已处理简报是对的，
    但没有这个出口就是死胡同。
    """
    reason = str(body.get("reason") or "").strip()
    with SessionLocal() as s:
        actor = str(body.get("actor") or "").strip() or seed_researcher(s).display_name
        try:
            brief_store.reopen(s, brief_id, actor=actor, reason=reason)
        except brief_store.BriefError as e:
            raise HTTPException(400, str(e)) from None
        return JSONResponse({"brief_id": brief_id, "status": "草稿", "actor": actor})


@app.post("/api/sn/review-task/{task_id}/resolve")
def review_task_resolve(task_id: int, body: dict = Body(...)):
    """关闭一条复盘待办。

    待办只增不减的话，首屏那块「少而紧急」几天就变成第二面数据墙——
    而它短，恰恰是首屏全部价值所在。
    """
    from tin.compute.signals import resolve_review_task

    note = str(body.get("note") or "").strip()
    with SessionLocal() as s:
        actor = str(body.get("actor") or "").strip() or seed_researcher(s).display_name
        try:
            task = resolve_review_task(s, task_id, actor=actor, note=note)
        except ValueError as e:
            raise HTTPException(400, str(e)) from None
        return JSONResponse({"task_id": task.id, "state": task.state, "actor": actor,
                             "resolved_at": task.resolved_at.isoformat()})


def _schema_state() -> tuple[str | None, str | None]:
    """（库里的 alembic 版本, 代码期望的 head）。取不到就返回 None，不让体检本身把进程弄挂。"""
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    from tin.config import ROOT

    try:
        head = ScriptDirectory.from_config(Config(str(ROOT / "alembic.ini"))).get_current_head()
    except Exception:
        head = None
    try:
        with SessionLocal() as s:
            current = s.execute(text("SELECT version_num FROM alembic_version")).scalar()
    except Exception:
        current = None
    return current, head


@app.get("/healthz", include_in_schema=False)
def healthz():
    """部署闸门。进程活着、数据库连得上、**且表结构是代码期望的那一版**。

    原来这里只做 `SELECT 1`：库落后两个迁移照样返回 ok，部署脚本据此判定成功，
    而应用其实一点开页面就 500。体检报平安、病人躺地上，是最坏的一种失败。
    所以版本不一致必须是 503——宁可让部署红，也不要让它假绿。
    """
    with SessionLocal() as s:
        s.execute(text("SELECT 1"))
    current, head = _schema_state()
    if head is not None and current != head:
        raise HTTPException(503, f"数据库表结构落后于代码：库内 {current or '无版本记录'}，"
                                 f"代码期望 {head}。请在该机器上执行 alembic upgrade head。")
    return {"status": "ok", "schema": current}


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
        # 跨来源校验原来挂在产业图页上，但它问的是"这条数据可不可信"，
        # 跟产业结构没关系——属于指标治理，放这里才找得到。
        day = latest_trade_date(s)
        checks = diagram_api.crosscheck(s, V, day) if day else []
        return templates.TemplateResponse(request, "indicators.html", _shell(
            s, "indicators", day, rows=rows, dimensions=DIMENSIONS, checks=checks))


@app.get("/sn/indicators/{series_id}")
def indicator_page(request: Request, series_id: str):
    """单指标页。

    指标列表回答不了研究员真正的问题——444 行里每一行看起来都一样重要。这一页要回答
    三件事：这个数现在多少且怎么来的（口径与凭证）、被谁在用（判断与产业图）、
    历史上有没有被改过（修订）。**「被谁在用」是分辨要不要维护它的唯一依据。**
    """
    with SessionLocal() as s:
        day = latest_trade_date(s)
        try:
            d = diagram_api.detail(s, series_id, day, limit=120)
        except diagram_api.DiagramError as e:
            raise HTTPException(404, str(e)) from e
        return templates.TemplateResponse(request, "indicator.html", _shell(
            s, "indicators", day, detail=d, usage=diagram_api.usage(s, series_id, V)))


@app.get("/api/sn/indicators/{series_id}/usage")
def indicator_usage(series_id: str):
    with SessionLocal() as s:
        return diagram_api.usage(s, series_id, V)


@app.get("/sn/diagram")
def diagram_page(request: Request, template: int | None = None):
    with SessionLocal() as s:
        # 这里原来无条件返回默认布局，`?template=` 被整个忽略——下拉框选哪张都回到同一张，
        # 新建的布局也永远打不开。切换布局是这个页面最基本的操作之一。
        default = diagram_api.ensure_default(s, V)
        rows = diagram_api.list_templates(s, V)
        tpl = next((t for t in rows if t["id"] == template), None) if template else None
        if template is not None and tpl is None:
            raise HTTPException(404, f"没有这张布局：{template}")
        return templates.TemplateResponse(request, "diagram.html", _shell(
            s, "diagram", latest_trade_date(s), template=tpl or default,
            templates_all=rows))


@app.get("/api/sn/diagram/values")
def diagram_values(template_id: int | None = None, date: str | None = None):
    with SessionLocal() as s:
        rows = diagram_api.list_templates(s, V)
        tpl = next((t for t in rows if t["id"] == template_id), None) or (rows[0] if rows else None)
        if tpl is None:
            raise HTTPException(404, "还没有任何布局")
        day = _board_date(s, date)
        return {"template_id": tpl["id"], "as_of": day.isoformat(),
                "judgment": diagram_api.judgment_roles(s, V)[1],
                "nodes": diagram_api.resolve(s, tpl["layout"], day)}


@app.post("/api/sn/diagram/values")
def diagram_values_live(body: dict = Body(...)):
    """按**前端当前正在编辑的布局**取值，不经过库里存的那一份。

    新增节点、刚换的绑定在保存之前库里并不存在；GET 版按 template_id 从库里读布局，
    于是新节点永远查不到值，看上去就是「绑了但不显示」。编辑态必须走这个入口。
    """
    layout = body.get("layout") or {}
    with SessionLocal() as s:
        day = _board_date(s, body.get("date"))
        roles_meta = diagram_api.judgment_roles(s, V)[1]
        return {"as_of": day.isoformat(), "judgment": roles_meta,
                "nodes": diagram_api.resolve(s, layout, day)}


@app.post("/api/sn/diagram/templates")
def diagram_save(body: dict = Body(...)):
    with SessionLocal() as s:
        try:
            saved = diagram_api.save_template(s, V, body)
        except diagram_api.DiagramError as e:
            raise HTTPException(400, str(e)) from None
        return {"saved": saved, "templates": diagram_api.list_templates(s, V)}


@app.get("/api/sn/diagram/markdown")
def diagram_to_markdown(template_id: int | None = None):
    """导出为 Markdown 大纲。绑定写成注释，改完再导回无损。"""
    with SessionLocal() as s:
        rows = diagram_api.list_templates(s, V)
        tpl = next((t for t in rows if t["id"] == template_id), None) or (rows[0] if rows else None)
        if tpl is None:
            raise HTTPException(404, "还没有任何布局")
        return {"name": tpl["name"], "markdown": diagram_md.dump(tpl["layout"], tpl["name"])}


@app.post("/api/sn/diagram/markdown")
def diagram_from_markdown(body: dict = Body(...)):
    """从 Markdown 大纲建一份新布局。不覆盖现有布局——导错了还能切回去。"""
    try:
        layout = diagram_md.parse(str(body.get("markdown", "")))
    except diagram_md.MarkdownError as e:
        raise HTTPException(400, str(e)) from None
    with SessionLocal() as s:
        # `into` 表示改写这一张，而不是新建。研究员在文本里改结构比拖方框快，
        # 但改完只能「导入为新布局」的话，每改一次就多一张图，改的还不是手里这张。
        into = body.get("into")
        name = str(body.get("name") or layout["name"]).strip()
        payload = {"name": name, "layout": layout}
        if into:
            row = next((t for t in diagram_api.list_templates(s, V) if t["id"] == into), None)
            if row is None:
                raise HTTPException(404, f"没有这张布局：{into}")
            payload["id"] = into
            payload["name"] = str(body.get("name") or row["name"]).strip()
        try:
            saved = diagram_api.save_template(s, V, payload)
        except diagram_api.DiagramError as e:
            raise HTTPException(400, str(e)) from None
        unbound = sum(1 for n in layout["nodes"]
                      if n.get("kind") != "group" and n["binding"]["kind"] == "none")
        return {"saved": saved, "unbound": unbound, "replaced": bool(into),
                "templates": diagram_api.list_templates(s, V)}


@app.get("/api/sn/diagram/crosscheck")
def diagram_crosscheck(date: str | None = None):
    with SessionLocal() as s:
        return {"checks": diagram_api.crosscheck(s, V, _board_date(s, date))}


@app.get("/api/sn/diagram/detail")
def diagram_detail(series_id: str, date: str | None = None):
    with SessionLocal() as s:
        try:
            return diagram_api.detail(s, series_id, _board_date(s, date))
        except diagram_api.DiagramError as e:
            raise HTTPException(404, str(e)) from None


@app.get("/api/sn/diagram/suggest")
def diagram_suggest(label: str, exclude: str | None = None):
    """给某个节点推荐候选指标。给不出就明说，不拿"有数据的热门指标"凑数。"""
    with SessionLocal() as s:
        return {"suggestions": diagram_api.suggest(s, V, label,
                                                   bound=set((exclude or "").split(",")) - {""})}


@app.delete("/api/sn/diagram/templates/{template_id}")
def diagram_delete(template_id: int):
    with SessionLocal() as s:
        try:
            return diagram_api.delete_template(s, V, template_id)
        except diagram_api.DiagramError as e:
            raise HTTPException(400, str(e)) from None


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


@app.get("/api/sn/series/{series_id}/history")
def api_series_history(series_id: str, date: str | None = None,
                       limit: int = Query(900, ge=2, le=2000)):
    with SessionLocal() as s:
        payload = macro_history(s, series_id, _board_date(s, date), limit)
        if payload is None:
            raise HTTPException(404, "该指标不支持历史图表")
        return JSONResponse(payload)
