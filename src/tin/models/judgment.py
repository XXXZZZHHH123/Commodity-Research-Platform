"""L2 判断单元与监测记录。"""

from datetime import date, datetime

from sqlalchemy import Date, ForeignKey, Index, Integer, String, Text, text
from sqlalchemy.orm import Mapped, mapped_column

from tin.db import Base, JSONType, UTCDateTime

JUDGMENT_STATUSES = ("草稿", "生效", "已到期", "已被证伪", "已归档")


class Judgment(Base):
    __tablename__ = "judgments"
    __table_args__ = (
        # 同一品种同一时间只有一条生效判断
        Index("uq_judgment_active", "variety", unique=True,
              sqlite_where=text("status = '生效'"), postgresql_where=text("status = '生效'")),
        Index("uq_judgment_version", "variety", "version", unique=True),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    variety: Mapped[str] = mapped_column(String(16))
    version: Mapped[int] = mapped_column(Integer)
    author: Mapped[str] = mapped_column(String(64))
    written_at: Mapped[date] = mapped_column(Date)
    review_period_days: Mapped[int] = mapped_column(Integer, default=30)
    review_due: Mapped[date] = mapped_column(Date)
    contradiction: Mapped[dict] = mapped_column(JSONType)
    marginal_focus: Mapped[list] = mapped_column(JSONType)
    cost_line: Mapped[dict | None] = mapped_column(JSONType)
    pricing_power: Mapped[dict] = mapped_column(JSONType)
    time_dimension: Mapped[dict | None] = mapped_column(JSONType)
    survey_notes: Mapped[str | None] = mapped_column(Text)
    whitelist: Mapped[list] = mapped_column(JSONType)
    paradigm: Mapped[str | None] = mapped_column(String(16))
    tone: Mapped[str | None] = mapped_column(String(16))
    tone_note: Mapped[str | None] = mapped_column(Text)
    thresholds: Mapped[list] = mapped_column(JSONType)
    priority_rule: Mapped[dict | None] = mapped_column(JSONType)
    falsifiers: Mapped[list] = mapped_column(JSONType)
    status: Mapped[str] = mapped_column(String(8), default="草稿")
    unstructured: Mapped[list] = mapped_column(JSONType, default=list)
    source_note: Mapped[str | None] = mapped_column(Text)
    review_warnings: Mapped[list] = mapped_column(JSONType, default=list)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime)


class JudgmentSeriesRef(Base):
    """判断对指标的每一处引用。判断本体存 JSON 快照，外键够不着，由这张表让数据库强制“只能引用已登记指标”。"""

    __tablename__ = "judgment_series_refs"

    judgment_id: Mapped[int] = mapped_column(ForeignKey("judgments.id", ondelete="CASCADE"), primary_key=True)
    ref_type: Mapped[str] = mapped_column(String(16), primary_key=True)  # whitelist/marginal_focus/threshold/falsifier
    ref_id: Mapped[str] = mapped_column(String(80), primary_key=True)
    series_id: Mapped[str] = mapped_column(ForeignKey("indicators.series_id"), index=True)


class Signal(Base):
    __tablename__ = "signals"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    judgment_id: Mapped[int] = mapped_column(ForeignKey("judgments.id"), index=True)
    rule_id: Mapped[str] = mapped_column(String(64))
    rule_type: Mapped[str] = mapped_column(String(16))
    trade_date: Mapped[str] = mapped_column(String(10))
    state: Mapped[str] = mapped_column(String(8))  # 未触发 / 触发 / 数据缺失 —— 没有“建议”字段
    current_value: Mapped[float | None]
    evidence: Mapped[list] = mapped_column(JSONType)
    gap_note: Mapped[str | None] = mapped_column(Text)
    confidence: Mapped[int] = mapped_column(Integer, default=100)
    generated_at: Mapped[datetime] = mapped_column(UTCDateTime)


class ReviewTask(Base):
    __tablename__ = "review_tasks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    judgment_id: Mapped[int] = mapped_column(ForeignKey("judgments.id"), index=True)
    trigger_type: Mapped[str] = mapped_column(String(16))
    signal_id: Mapped[int | None] = mapped_column(ForeignKey("signals.id"))
    assignee: Mapped[str] = mapped_column(String(64))
    state: Mapped[str] = mapped_column(String(8), default="待处理")
    resolution_note: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime)
    resolved_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
