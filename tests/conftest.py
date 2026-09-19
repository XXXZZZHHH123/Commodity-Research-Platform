import json
from datetime import date, datetime
from pathlib import Path

import pytest
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import tin.models  # noqa: F401
from tin.config import SHANGHAI
from tin.db import Base, make_engine
from tin.ingest.record import record
from tin.ingest.runner import store
from tin.ingest.shfe import parse_daily_warrant, parse_quotes, parse_weekly_stock
from tin.jobs.seed import seed_indicators
from tin.models import TradingDay
from tin.schemas.caliber import Caliber
from tin.schemas.observation import ObservationIn

FIX = Path(__file__).parent / "fixtures"
D = date(2026, 9, 18)


@pytest.fixture
def session():
    engine = make_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    with sessionmaker(engine, expire_on_commit=False)() as s:
        seed_indicators(s)
        yield s


@pytest.fixture
def loaded(session):
    """9/18 的真实行情、仓单、周库存 + 9/17 仓单（交易所公布值 5221），交易日历 173/174。"""
    store(session, parse_quotes(json.loads((FIX / "shfe_kx_20260918.json").read_text())))
    store(session, parse_daily_warrant((FIX / "shfe_dailystock_20260918.html").read_text(encoding="utf-8"), D))
    store(session, parse_weekly_stock((FIX / "shfe_weeklystock_20260918.html").read_text(encoding="utf-8"), D))
    session.add(TradingDay(trade_date="2026-09-17", year=2026, year_seq=173))
    record(session, ObservationIn(
        series_id="SHFE.SN.warrant", value=5221, as_of=datetime(2026, 9, 17, 15, tzinfo=SHANGHAI),
        caliber=Caliber(time_type="交易时点", stock_scope="交易所注册仓单"), source="SHFE"))
    session.commit()
    return session


def enter_spot(session, value: float, when: datetime):
    record(session, ObservationIn(
        series_id="SMM.SN.spot.1", value=value, as_of=when,
        caliber=Caliber(time_type="发布时点", price_type="均价", spot_source="SMM"),
        source="人工", entered_by="测试", note="测试录入"))
    session.commit()
