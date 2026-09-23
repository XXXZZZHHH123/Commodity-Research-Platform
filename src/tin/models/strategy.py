"""Strategy plans and append-only lifecycle history."""
from datetime import datetime

from sqlalchemy import ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from tin.db import Base, JSONType, UTCDateTime


class Strategy(Base):
    __tablename__ = "strategies"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    title: Mapped[str] = mapped_column(String(200))
    variety: Mapped[str] = mapped_column(String(16), index=True)
    kind: Mapped[str] = mapped_column(String(32))
    mode: Mapped[str] = mapped_column(String(16))
    status: Mapped[str] = mapped_column(String(16), index=True)
    plan: Mapped[dict] = mapped_column(JSONType)
    author: Mapped[str] = mapped_column(String(64))
    revision_of: Mapped[int | None] = mapped_column(ForeignKey("strategies.id"))
    version: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime)
    published_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    opened_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    closed_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    entry_fills: Mapped[list] = mapped_column(JSONType, default=list)
    exit_fills: Mapped[list] = mapped_column(JSONType, default=list)
    __mapper_args__ = {"version_id_col": version}


class StrategyEvent(Base):
    __tablename__ = "strategy_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    strategy_id: Mapped[int] = mapped_column(ForeignKey("strategies.id"), index=True)
    event_type: Mapped[str] = mapped_column(String(32))
    actor: Mapped[str] = mapped_column(String(64))
    reason: Mapped[str] = mapped_column(Text)
    at: Mapped[datetime] = mapped_column(UTCDateTime)
    payload: Mapped[dict] = mapped_column(JSONType)

