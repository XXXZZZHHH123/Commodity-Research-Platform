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


def _real_db_at(path, revision: str):
    """真的把一个库升到某一版——不是只盖个章。

    只写 alembic_version 而不建表，造出来的是现实中不存在的状态：后续迁移
    `alter table judgments` 会找不到表。要验自动升级，库就得是真的。
    """
    from alembic import command
    from alembic.config import Config

    cfg = Config()
    cfg.set_main_option("script_location", str(schema_check.ROOT / "alembic"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{path}")
    command.upgrade(cfg, revision)
    return make_engine(f"sqlite:///{path}")


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


# ---------- 自动迁移 ----------

def test_auto_upgrade_brings_a_stale_database_to_head(tmp_path):
    """部署脚本和容器启动都没有迁移步骤，不自动跑就得有人手动上服务器执行。"""
    db = tmp_path / "stale.db"
    engine = _real_db_at(db, "6c0c0443dc71")

    result = schema_check.auto_upgrade(engine, keep_backups=3)

    assert result["ran"] and result["ok"], result.get("error")
    assert schema_check.migration_gap(make_engine(f"sqlite:///{db}")) is None


def test_auto_upgrade_backs_up_before_touching_anything(tmp_path):
    """自动改生产库之所以敢开，前提就是这一步：出问题有东西可退。"""
    db = tmp_path / "stale.db"
    engine = _real_db_at(db, "6c0c0443dc71")

    saved = schema_check.auto_upgrade(engine, keep_backups=3)["backup"]

    assert saved is not None and saved.exists()
    assert "before-migrate" in saved.name
    assert schema_check.db_revisions(make_engine(f"sqlite:///{saved}")) == {"6c0c0443dc71"}, \
        "备份必须是迁移**之前**的样子，否则退不回去"


def test_backups_are_pruned_so_they_do_not_pile_up(tmp_path):
    db = tmp_path / "x.db"
    engine = make_engine(f"sqlite:///{db}")
    _stamp(engine, "6c0c0443dc71")
    for _ in range(4):
        schema_check.backup(engine, keep=2)
    assert len(list(tmp_path.glob("x.before-migrate-*.db"))) <= 2


def test_a_database_at_head_is_not_backed_up_or_touched(tmp_path):
    """已经是最新就什么都不做——每次重启都复制一份库文件是纯浪费。"""
    db = tmp_path / "ok.db"
    engine = make_engine(f"sqlite:///{db}")
    Base.metadata.create_all(engine)
    _stamp(engine, sorted(schema_check.script_heads())[0])

    assert schema_check.auto_upgrade(engine) == {"ran": False, "ok": True}
    assert not list(tmp_path.glob("*before-migrate*"))


def test_a_failed_upgrade_reports_instead_of_raising(tmp_path, monkeypatch):
    """启动时迁移失败如果直接让进程退出，得到的是起不来的服务和一段终端里的堆栈。

    返回失败让应用照常起来，由 503 页面把错误和手动命令一起显示出来——人看得见才修得了。
    """
    db = tmp_path / "boom.db"
    engine = make_engine(f"sqlite:///{db}")
    _stamp(engine, "6c0c0443dc71")

    def explode(*a, **k):
        raise RuntimeError("磁盘满了")

    monkeypatch.setattr("alembic.command.upgrade", explode)
    result = schema_check.auto_upgrade(engine)

    assert result["ran"] and not result["ok"]
    assert "磁盘满了" in result["error"]
    assert result["backup"] is not None, "失败时更需要那份备份"


def test_upgrading_does_not_wipe_out_the_application_logging(tmp_path):
    """alembic 的 env.py 一见到 ini 就会 fileConfig()，那会禁用已有的 logger——
    uvicorn 的访问日志会从此一条不出。所以不能把 alembic.ini 传进去。"""
    import logging

    db = tmp_path / "log.db"
    engine = make_engine(f"sqlite:///{db}")
    _stamp(engine, "6c0c0443dc71")
    probe = logging.getLogger("tin.probe.logging")

    schema_check.auto_upgrade(engine)

    assert not probe.disabled, "升级过程不该把应用的 logger 关掉"
