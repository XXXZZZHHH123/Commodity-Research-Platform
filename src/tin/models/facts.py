"""L1 事实底座：只存事实，不产生判断。"""

from datetime import datetime, time

from sqlalchemy import Boolean, Float, ForeignKey, Index, Integer, String, Text, Time, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from tin.db import Base, JSONType, UTCDateTime


class Indicator(Base):
    __tablename__ = "indicators"

    series_id: Mapped[str] = mapped_column(String(80), primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    variety: Mapped[str] = mapped_column(String(16), index=True)
    category: Mapped[str] = mapped_column(String(16))
    caliber: Mapped[dict] = mapped_column(JSONType)
    unit: Mapped[str] = mapped_column(String(16))
    source: Mapped[str] = mapped_column(String(32))
    source_url: Mapped[str | None] = mapped_column(Text)
    frequency: Mapped[str] = mapped_column(String(8))
    fetch_mode: Mapped[str] = mapped_column(String(8))
    phase: Mapped[str] = mapped_column(String(4), default="P1")
    owner: Mapped[str | None] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(8), default="可用")
    is_proxy: Mapped[bool] = mapped_column(Boolean, default=False)
    vendor_code: Mapped[str | None] = mapped_column(String(32))
    note: Mapped[str | None] = mapped_column(Text)
    # 该指标的常规发布时刻。录入页、快速录入抽屉与 Excel 导入共用同一个源头，
    # 避免"系统替人编造时点"时三处默认值各不相同。
    default_entry_time: Mapped[time | None] = mapped_column(Time)


class Observation(Base):
    """只存真实取到的数。取数失败记在 fetch_runs，不在这里写 NULL 值行。"""

    __tablename__ = "observations"
    __table_args__ = (
        UniqueConstraint("series_id", "as_of", "revision"),
        Index("ix_obs_series_asof", "series_id", "as_of"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    series_id: Mapped[str] = mapped_column(ForeignKey("indicators.series_id"))
    value: Mapped[float] = mapped_column(Float)
    as_of: Mapped[datetime] = mapped_column(UTCDateTime, index=True)
    fetched_at: Mapped[datetime] = mapped_column(UTCDateTime)
    caliber_snapshot: Mapped[dict] = mapped_column(JSONType)
    source: Mapped[str] = mapped_column(String(32))
    source_url: Mapped[str | None] = mapped_column(Text)
    revision: Mapped[int] = mapped_column(Integer, default=0)
    entered_by: Mapped[str | None] = mapped_column(String(32))
    note: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(8), default="正常")


class Derived(Base):
    __tablename__ = "derived"
    __table_args__ = (Index("ix_derived_formula_asof", "formula_id", "variety", "trade_date"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    formula_id: Mapped[str] = mapped_column(String(32))
    variety: Mapped[str] = mapped_column(String(16))
    trade_date: Mapped[str] = mapped_column(String(10))
    value: Mapped[float | None] = mapped_column(Float)
    as_of: Mapped[datetime | None] = mapped_column(UTCDateTime)
    inputs: Mapped[list] = mapped_column(JSONType)
    params: Mapped[dict] = mapped_column(JSONType, default=dict)
    status: Mapped[str] = mapped_column(String(32))
    note: Mapped[str | None] = mapped_column(Text)
    computed_at: Mapped[datetime] = mapped_column(UTCDateTime)


class FetchRun(Base):
    __tablename__ = "fetch_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    fetcher: Mapped[str] = mapped_column(String(32), index=True)
    target_date: Mapped[str] = mapped_column(String(10))
    started_at: Mapped[datetime] = mapped_column(UTCDateTime)
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    status: Mapped[str] = mapped_column(String(8))
    attempts: Mapped[int] = mapped_column(Integer, default=1)
    written: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(Text)


class TradingDay(Base):
    """上期所交易日历，由行情日报的年内期号（o_year_num）建立；期号不连续即说明中间有交易日缺数。"""

    __tablename__ = "trading_days"

    trade_date: Mapped[str] = mapped_column(String(10), primary_key=True)
    year: Mapped[int] = mapped_column(Integer)
    year_seq: Mapped[int] = mapped_column(Integer)
