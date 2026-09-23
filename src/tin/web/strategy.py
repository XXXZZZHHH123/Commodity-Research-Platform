"""Strategy HTTP adapter. Decisions and accounting live in tin.strategy.service."""

from datetime import date, datetime

from fastapi import APIRouter, Body, HTTPException, Request
from pydantic import ValidationError

from tin.compute.engine import latest_trade_date
from tin.config import SHANGHAI
from tin.jobs.seed import seed_researcher
from tin.schemas.strategy import ExecutionIn, StrategyPlan
from tin.strategy import service

STATUS_LABELS = {"draft": "草稿", "watching": "等待入场", "open": "跟踪中",
                 "closed": "已退出", "cancelled": "已作废"}
MODE_LABELS = {"simulation": "模拟跟踪", "actual": "实际成交记录"}
KIND_LABELS = {"outright": "方向策略", "calendar_spread": "跨期价差"}


def _validation_message(exc: ValidationError) -> str:
    return "；".join(f"{'.'.join(map(str, e['loc'])) or '策略'}：{e['msg']}"
                    for e in exc.errors(include_url=False))


def strategy_summary(session) -> dict:
    rows = service.list_strategies(session)
    active = [row for row in rows if row.status in ("watching", "open")]
    return {"active": len(active), "drafts": sum(row.status == "draft" for row in rows),
            "items": [{"id": row.id, "title": row.title, "status": STATUS_LABELS[row.status],
                       "mode": MODE_LABELS[row.mode]} for row in active[:4]]}


def build_router(session_factory, templates, shell) -> APIRouter:
    """Use a lazy session factory so the application's session override stays effective."""
    router = APIRouter()

    def context(session, **extra):
        return shell(session, "strategies", None, status_labels=STATUS_LABELS,
                     mode_labels=MODE_LABELS, kind_labels=KIND_LABELS,
                     now_local=datetime.now(SHANGHAI).strftime("%Y-%m-%dT%H:%M"), **extra)

    def find(session, strategy_id):
        row = service.get_strategy(session, strategy_id)
        if row is None:
            raise HTTPException(404, "策略不存在")
        return row

    def mutate(strategy_id, body, operation):
        with session_factory() as session:
            find(session, strategy_id)
            actor = seed_researcher(session).display_name
            try:
                if operation in ("open", "close"):
                    execution = ExecutionIn.model_validate(body)
                    getattr(service, f"{operation}_strategy")(session, strategy_id, execution, actor=actor)
                else:
                    getattr(service, f"{operation}_strategy")(
                        session, strategy_id, actor=actor, reason=str(body.get("reason") or "").strip())
                session.commit()
                return service.serialize_strategy(session, find(session, strategy_id))
            except ValidationError as exc:
                session.rollback()
                raise HTTPException(400, _validation_message(exc)) from None
            except ValueError as exc:
                session.rollback()
                raise HTTPException(400, str(exc)) from None

    @router.get("/sn/strategies")
    def strategies_page(request: Request, status: str = "all"):
        if status != "all" and status not in STATUS_LABELS:
            raise HTTPException(400, "未知策略状态")
        with session_factory() as session:
            rows = service.list_strategies(session)
            visible = [row for row in rows if status == "all" or row.status == status]
            return templates.TemplateResponse(request, "strategies.html", context(
                session, strategies=[service.serialize_strategy(session, row) for row in visible],
                selected_status=status, summary=strategy_summary(session),
                latest_date=latest_trade_date(session)))

    @router.get("/sn/strategies/{strategy_id}")
    def strategy_page(request: Request, strategy_id: int):
        with session_factory() as session:
            return templates.TemplateResponse(request, "strategy_detail.html", context(
                session, strategy=service.serialize_strategy(session, find(session, strategy_id))))

    @router.get("/api/sn/strategies")
    def strategies_api():
        with session_factory() as session:
            return {"strategies": [service.serialize_strategy(session, row)
                                   for row in service.list_strategies(session)]}

    @router.post("/api/sn/strategies", status_code=201)
    def create_strategy(body: dict = Body(...)):
        with session_factory() as session:
            try:
                plan = StrategyPlan.model_validate(body)
                row = service.create_strategy(session, plan, actor=seed_researcher(session).display_name,
                                              reason="研究员建立策略草稿")
                session.commit()
                return service.serialize_strategy(session, row)
            except ValidationError as exc:
                session.rollback()
                raise HTTPException(400, _validation_message(exc)) from None
            except ValueError as exc:
                session.rollback()
                raise HTTPException(400, str(exc)) from None

    @router.post("/api/sn/strategies/generate")
    def generate_strategy(body: dict = Body(...)):
        from tin.strategy.generate import generate_candidates

        with session_factory() as session:
            requested = body.get("date")
            try:
                day = date.fromisoformat(requested) if requested else latest_trade_date(session)
            except (TypeError, ValueError):
                raise HTTPException(400, "研究日期应为 YYYY-MM-DD") from None
            if day is None:
                return {"status": "no_candidate", "strategies": [],
                        "gaps": ["尚无行情数据。先录入明确合约的行情，再生成策略草稿。"]}
            if day > datetime.now(SHANGHAI).date():
                raise HTTPException(400, "研究日期不能晚于今天")
            result = generate_candidates(session, "SN", day,
                                         researcher_id=seed_researcher(session).id,
                                         research_question=str(body.get("research_question") or "").strip())
            session.commit()
            return {"status": result.status, "gaps": result.gaps, "call_id": result.call_id,
                    "strategies": [service.serialize_strategy(session, row) for row in result.strategies]}

    @router.get("/api/sn/strategies/{strategy_id}")
    def strategy_api(strategy_id: int):
        with session_factory() as session:
            return service.serialize_strategy(session, find(session, strategy_id))

    @router.post("/api/sn/strategies/{strategy_id}/publish")
    def publish_strategy(strategy_id: int, body: dict = Body(...)):
        return mutate(strategy_id, body, "publish")

    @router.post("/api/sn/strategies/{strategy_id}/open")
    def open_strategy(strategy_id: int, body: dict = Body(...)):
        return mutate(strategy_id, body, "open")

    @router.post("/api/sn/strategies/{strategy_id}/close")
    def close_strategy(strategy_id: int, body: dict = Body(...)):
        return mutate(strategy_id, body, "close")

    @router.post("/api/sn/strategies/{strategy_id}/cancel")
    def cancel_strategy(strategy_id: int, body: dict = Body(...)):
        return mutate(strategy_id, body, "cancel")

    return router
