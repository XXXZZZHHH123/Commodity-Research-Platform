"""导出模板的增删改查（方案 05 §2.2.2 / §7.3）。

一期没有登录体系，模板按品种共享；同一品种只允许一个默认模板，由部分唯一索引兜底。
"""

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from tin.models import ExportTemplate

VALID_KINDS = ("meta", "observation", "derived", "judgment")


class TemplateError(ValueError):
    pass


def _clean_columns(columns: list[dict]) -> list[dict]:
    if not columns:
        raise TemplateError("模板至少要包含一列")
    out = []
    for col in columns:
        kind = col.get("kind")
        if kind not in VALID_KINDS:
            raise TemplateError(f"列 {col.get('field')} 缺少合法的 kind（{'/'.join(VALID_KINDS)}）")
        out.append({"field": str(col["field"]), "label": str(col.get("label") or col["field"]), "kind": kind})
    return out


def to_dict(row: ExportTemplate) -> dict:
    return {"id": row.id, "name": row.name, "is_default": row.is_default, "columns": row.columns,
            "date_range": row.date_range, "start_date": row.start_date, "end_date": row.end_date,
            "sort_order": row.sort_order, "updated_at": row.updated_at.isoformat()}


def list_templates(session: Session, variety: str) -> list[dict]:
    rows = session.scalars(select(ExportTemplate).where(ExportTemplate.variety == variety)
                           .order_by(ExportTemplate.is_default.desc(),
                                     ExportTemplate.updated_at.desc())).all()
    return [to_dict(r) for r in rows]


def save_template(session: Session, variety: str, payload: dict) -> dict:
    name = str(payload.get("name", "")).strip()
    if not name:
        raise TemplateError("请填写模板名称")
    columns = _clean_columns(payload.get("columns") or [])
    now = datetime.now(timezone.utc)

    row = session.get(ExportTemplate, payload["id"]) if payload.get("id") else None
    if row is not None and row.variety != variety:
        raise TemplateError("模板不属于当前品种")
    if row is None:
        exists = session.scalars(select(ExportTemplate)
                                 .where(ExportTemplate.variety == variety,
                                        ExportTemplate.name == name)).first()
        if exists is not None:
            raise TemplateError(f"已存在同名模板「{name}」，请改名或选择覆盖保存")
        row = ExportTemplate(variety=variety, created_at=now,
                             is_default=not list_templates(session, variety))  # 首个模板自动成为默认
        session.add(row)

    row.name, row.columns, row.updated_at = name, columns, now
    row.date_range = payload.get("date_range") or "recent_30_trade_days"
    row.start_date, row.end_date = payload.get("start_date"), payload.get("end_date")
    row.sort_order = payload.get("sort_order") or "desc"
    if payload.get("is_default"):
        set_default(session, variety, row)
    session.commit()
    return to_dict(row)


def set_default(session: Session, variety: str, row: ExportTemplate) -> None:
    for other in session.scalars(select(ExportTemplate).where(ExportTemplate.variety == variety)):
        other.is_default = other is row
    session.flush()


def delete_template(session: Session, variety: str, template_id: int) -> dict:
    row = session.get(ExportTemplate, template_id)
    if row is None or row.variety != variety:
        raise TemplateError("模板不存在")
    was_default = row.is_default
    session.delete(row)
    session.flush()
    # 默认模板被删后，把剩余模板中最近更新的一个顶上，避免「默认模板永远删不掉」的死角
    if was_default:
        heir = session.scalars(select(ExportTemplate).where(ExportTemplate.variety == variety)
                               .order_by(ExportTemplate.updated_at.desc()).limit(1)).first()
        if heir is not None:
            heir.is_default = True
    session.commit()
    return {"deleted": template_id, "templates": list_templates(session, variety)}
