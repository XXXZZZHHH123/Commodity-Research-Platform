from datetime import datetime

from sqlalchemy import Boolean, Index, Integer, String, text
from sqlalchemy.orm import Mapped, mapped_column

from tin.db import Base, JSONType, UTCDateTime


class User(Base):
    __tablename__ = "users"

    username: Mapped[str] = mapped_column(String(32), primary_key=True)
    display_name: Mapped[str] = mapped_column(String(32))
    password_hash: Mapped[str] = mapped_column(String(256))


class AuditLog(Base):
    """操作留痕（FR-8.2）：判断增改、人工录入、证伪处理。只增不删。"""

    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    at: Mapped[datetime] = mapped_column(UTCDateTime, index=True)
    actor: Mapped[str] = mapped_column(String(32))
    action: Mapped[str] = mapped_column(String(32))
    target_type: Mapped[str] = mapped_column(String(32))
    target_id: Mapped[str] = mapped_column(String(80))
    detail: Mapped[dict] = mapped_column(JSONType, default=dict)


class ExportTemplate(Base):
    """导出模板。一期没有登录体系，按品种共享；同一品种只允许一个默认模板。"""

    __tablename__ = "export_templates"
    __table_args__ = (
        Index("uq_export_template_default", "variety", unique=True,
              sqlite_where=text("is_default = 1"), postgresql_where=text("is_default = TRUE")),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    variety: Mapped[str] = mapped_column(String(16), index=True)
    name: Mapped[str] = mapped_column(String(64))
    is_default: Mapped[bool] = mapped_column(Boolean, default=False)
    # [{"field": ..., "label": ..., "kind": "meta|observation|derived|judgment"}]
    # kind 显式声明，导出取数按它分派，不靠字符串猜测字段类型
    columns: Mapped[list] = mapped_column(JSONType)
    date_range: Mapped[str] = mapped_column(String(32), default="recent_30_trade_days")
    start_date: Mapped[str | None] = mapped_column(String(10))
    end_date: Mapped[str | None] = mapped_column(String(10))
    sort_order: Mapped[str] = mapped_column(String(16), default="desc")
    created_at: Mapped[datetime] = mapped_column(UTCDateTime)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime)


class ImportPreview(Base):
    """导入预览的服务端权威缓存：commit 只认 preview_id，不接收前端回传的数据行。"""

    __tablename__ = "import_previews"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    variety: Mapped[str] = mapped_column(String(16), index=True)
    actor: Mapped[str] = mapped_column(String(32))
    filename: Mapped[str] = mapped_column(String(256))
    # 只存入库所需的最小集合，不保留原始单元格全文或整表副本
    payload: Mapped[dict] = mapped_column(JSONType)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, index=True)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime, index=True)
