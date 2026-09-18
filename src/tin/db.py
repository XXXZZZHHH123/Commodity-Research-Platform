import sqlite3
from datetime import datetime, timezone

from sqlalchemy import JSON, DateTime, create_engine, event
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker
from sqlalchemy.types import TypeDecorator

from tin.config import settings

JSONType = JSON().with_variant(JSONB(), "postgresql")


class UTCDateTime(TypeDecorator):
    """带时区的时间统一存 UTC；拒绝 naive datetime，逼调用方显式声明时区。"""

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect):
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError(f"拒绝入库不带时区的时间：{value!r}")
        value = value.astimezone(timezone.utc)
        return value.replace(tzinfo=None) if dialect.name == "sqlite" else value

    def process_result_value(self, value: datetime | None, dialect):
        if value is None:
            return None
        return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


class Base(DeclarativeBase):
    pass


def _enable_sqlite_fk(dbapi_conn, _record):
    # SQLite 默认不强制外键；不打开的话"证伪必须绑定已登记指标"形同虚设
    if isinstance(dbapi_conn, sqlite3.Connection):
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()


def make_engine(url: str | None = None, **kwargs) -> Engine:
    engine = create_engine(url or settings.database_url, **kwargs)
    event.listen(engine, "connect", _enable_sqlite_fk)
    return engine


engine = make_engine()
SessionLocal = sessionmaker(engine, expire_on_commit=False)
