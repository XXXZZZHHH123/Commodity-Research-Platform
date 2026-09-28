"""库落后于代码时，整个站点该说人话，而不是甩 SQL 堆栈。

这个坑绊了三次：拉了新代码没跑迁移，然后
  `no such table: diagram_templates` → 产业图页 500
  `no such table: researchers`       → 首屏 500
每次都要有人读懂 SQLAlchemy 的报错格式才能推出"该跑 alembic upgrade head"。

第一次只给 diagram_templates 加了 try/except，第二次就换成 researchers 又炸。
**按表逐个兜是补不完的**，所以改成统一检查迁移版本。这一组测试守的就是这件事。
"""

from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import sessionmaker

import tin.models  # noqa: F401
from tin import schema_check
from tin.db import Base, make_engine
from tin.web.app import app


def _fresh_engine():
    from sqlalchemy.pool import StaticPool

    engine = make_engine("sqlite://", poolclass=StaticPool,
                         connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    return engine


def _stamp(engine, revision: str):
    """把库标成某个迁移版本，模拟"跑过迁移"的真实库。"""
    with engine.begin() as conn:
        conn.execute(text("create table if not exists alembic_version "
                          "(version_num varchar(32) not null)"))
        conn.execute(text("delete from alembic_version"))
        conn.execute(text("insert into alembic_version values (:v)"), {"v": revision})


def test_a_database_at_head_is_not_flagged():
    engine = _fresh_engine()
    _stamp(engine, sorted(schema_check.script_heads())[0])
    assert schema_check.migration_gap(engine) is None


def test_a_database_left_behind_says_exactly_what_to_run():
    engine = _fresh_engine()
    _stamp(engine, "6c0c0443dc71")          # Excel 导入导出那一版，早已不是 head
    gap = schema_check.migration_gap(engine)
    assert gap and "alembic upgrade head" in gap
    assert "6c0c0443dc71" in gap, "要说清楚停在哪一版，否则不知道差多少"
    assert "不删除、不改写任何已有数据" in gap, "不说清影响面，没人敢跑迁移"


def test_a_create_all_database_is_left_alone():
    """测试库与全新安装走 create_all，没有 alembic_version。

    这种库的表是齐的，把它判成"落后"会让整个测试套跑不起来——
    这个检查宁可漏报，也不能误报。
    """
    assert schema_check.migration_gap(_fresh_engine()) is None


def test_no_engine_means_no_opinion():
    """拿不到 engine（测试里 SessionLocal 被换成普通 lambda）时直接放行。

    **不能退而去开一个 session**：测试共用同一个 session，`with` 一包就会回滚掉
    夹具数据。
    """
    assert schema_check.engine_of(lambda: None) is None
    assert schema_check.migration_gap(None) is None


def test_every_page_is_guarded_not_just_the_one_that_broke_last_time(monkeypatch):
    """落后时**所有**页面都给同一句话，接口给 503——而不是等它在某张表上炸开。"""
    engine = _fresh_engine()
    _stamp(engine, "6c0c0443dc71")
    monkeypatch.setattr("tin.web.app.SessionLocal",
                        sessionmaker(engine, expire_on_commit=False))
    client = TestClient(app, raise_server_exceptions=False)

    for path in ("/", "/sn", "/sn/judgment", "/sn/indicators", "/sn/diagram", "/sn/entry"):
        r = client.get(path)
        assert r.status_code == 503, f"{path} 应当拦住"
        assert "alembic upgrade head" in r.text, f"{path} 没说该跑什么"

    r = client.get("/api/sn/diagram/values?template_id=1")
    assert r.status_code == 503 and "alembic upgrade head" in r.json()["detail"]

    # 静态资源要放行，否则那张提示页自己没有样式
    assert client.get("/static/app.css").status_code == 200


def test_the_guard_steps_aside_once_the_database_catches_up(monkeypatch):
    engine = _fresh_engine()
    _stamp(engine, sorted(schema_check.script_heads())[0])
    monkeypatch.setattr("tin.web.app.SessionLocal",
                        sessionmaker(engine, expire_on_commit=False))
    r = TestClient(app, raise_server_exceptions=False).get("/sn/indicators")
    assert r.status_code != 503, "追上之后不该再拦"
