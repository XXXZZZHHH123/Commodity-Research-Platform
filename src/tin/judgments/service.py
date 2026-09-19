from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from tin.judgments.validate import activation_blockers
from tin.models import AuditLog, Indicator, Judgment, JudgmentSeriesRef
from tin.schemas.judgment import JudgmentIn


class JudgmentError(ValueError):
    pass


def _refs(j: JudgmentIn) -> list[tuple[str, str, str]]:
    refs = [("whitelist", sid, sid) for sid in j.whitelist]
    refs += [("marginal_focus", f"mf{i}", m.series_id) for i, m in enumerate(j.marginal_focus) if m.series_id]
    refs += [("threshold", t.id, t.series_id) for t in j.thresholds]
    refs += [("falsifier", f.id, f.series_id) for f in j.falsifiers]
    for side in ("bull", "bear"):
        refs += [("evidence", f"{side}{i}", e.series_id)
                 for i, e in enumerate(getattr(j.contradiction, side).evidence) if e.series_id]
    return refs


def save_version(session: Session, j: JudgmentIn, *, actor: str, activate: bool = False) -> Judgment:
    """每次保存都生成新版本（FR-1.5）。activate=True 时须通过全部硬校验，且旧的生效版本转为已归档。"""
    refs = _refs(j)
    wanted = {sid for _t, _i, sid in refs}
    known = set(session.scalars(select(Indicator.series_id).where(Indicator.series_id.in_(wanted))))
    if wanted - known:
        raise JudgmentError(f"引用了未登记的指标：{', '.join(sorted(wanted - known))}（请先在指标管理页登记）")

    blockers = activation_blockers(j)
    if activate and blockers:
        raise JudgmentError("不能生效：" + "；".join(blockers))

    version = (session.scalar(select(func.max(Judgment.version)).where(Judgment.variety == j.variety)) or 0) + 1
    if activate:
        session.execute(update(Judgment)
                        .where(Judgment.variety == j.variety, Judgment.status == "生效")
                        .values(status="已归档"))
    data = j.model_dump(mode="json", by_alias=True)
    row = Judgment(
        variety=j.variety, version=version, author=j.author, written_at=j.written_at,
        review_period_days=j.review_period_days,
        review_due=j.written_at + timedelta(days=j.review_period_days),
        contradiction=data["contradiction"], marginal_focus=data["marginal_focus"],
        cost_line=data["cost_line"], pricing_power=data["pricing_power"],
        time_dimension=data["time_dimension"], survey_notes=j.survey_notes, whitelist=j.whitelist,
        paradigm=j.paradigm, tone=j.tone, tone_note=j.tone_note, thresholds=data["thresholds"],
        priority_rule=data["priority_rule"], falsifiers=data["falsifiers"],
        unstructured=data["unstructured"], source_note=j.source_note,
        status="生效" if activate else "草稿", review_warnings=[],
        created_at=datetime.now(timezone.utc),
    )
    session.add(row)
    session.flush()
    session.add_all(JudgmentSeriesRef(judgment_id=row.id, ref_type=t, ref_id=i, series_id=sid)
                    for t, i, sid in refs)
    session.add(AuditLog(at=datetime.now(timezone.utc), actor=actor, action="判断保存",
                         target_type="judgment", target_id=f"{j.variety}v{version}",
                         detail={"status": row.status, "blockers": blockers}))
    session.flush()
    return row


def to_payload(row: Judgment) -> JudgmentIn:
    return JudgmentIn.model_validate({
        "variety": row.variety, "author": row.author, "written_at": row.written_at,
        "review_period_days": row.review_period_days, "contradiction": row.contradiction,
        "marginal_focus": row.marginal_focus, "cost_line": row.cost_line,
        "pricing_power": row.pricing_power, "time_dimension": row.time_dimension,
        "survey_notes": row.survey_notes, "whitelist": row.whitelist, "paradigm": row.paradigm,
        "tone": row.tone, "tone_note": row.tone_note, "thresholds": row.thresholds,
        "priority_rule": row.priority_rule, "falsifiers": row.falsifiers,
        "unstructured": row.unstructured, "source_note": row.source_note,
    })


def current(session: Session, variety: str) -> Judgment | None:
    """看板展示用：优先生效版本，没有则取最新草稿。"""
    active = session.scalars(select(Judgment).where(Judgment.variety == variety, Judgment.status == "生效")).first()
    if active:
        return active
    return session.scalars(select(Judgment).where(Judgment.variety == variety)
                           .order_by(Judgment.version.desc()).limit(1)).first()
