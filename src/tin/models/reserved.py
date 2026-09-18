"""三四期表（04 §1.7 / §1.8）。一期建表不用，避免后期迁移。"""

from datetime import date

from sqlalchemy import Date, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from tin.db import Base, JSONType


class Factor(Base):
    __tablename__ = "factors"

    factor_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    name: Mapped[str] = mapped_column(String(64))
    description: Mapped[str | None] = mapped_column(Text)
    proxy_series: Mapped[list] = mapped_column(JSONType, default=list)
    alert_rule: Mapped[dict | None] = mapped_column(JSONType)


class JudgmentFactorLink(Base):
    __tablename__ = "judgment_factor_link"

    judgment_id: Mapped[int] = mapped_column(ForeignKey("judgments.id"), primary_key=True)
    factor_id: Mapped[str] = mapped_column(ForeignKey("factors.factor_id"), primary_key=True)
    direction: Mapped[str] = mapped_column(String(8))
    weight_hint: Mapped[str | None] = mapped_column(String(32))


class MacroScenario(Base):
    __tablename__ = "macro_scenarios"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    version: Mapped[int] = mapped_column(Integer)
    author: Mapped[str] = mapped_column(String(64))
    regime: Mapped[str] = mapped_column(String(32))
    variables: Mapped[list] = mapped_column(JSONType)
    effective_from: Mapped[date] = mapped_column(Date)
    status: Mapped[str] = mapped_column(String(8))
