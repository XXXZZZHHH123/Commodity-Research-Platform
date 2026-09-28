"""库的表结构跟代码对不对得上。

同一个坑绊了三次：拉了新代码、没跑迁移，然后页面甩一段 SQL 堆栈的 500 出来——
`no such table: diagram_templates`、`no such table: researchers`。每次都要有人
读懂 SQLAlchemy 的报错格式，才能推出"该跑 alembic upgrade head"。

第一次我只给产业图那一张表做了友好提示，于是第二次换成 `researchers` 又是 500。
**按表逐个 try/except 是补不完的**——每加一张表就多一个坑。所以改成一次性检查
库的迁移版本：对不上就在**所有页面**给同一句话，而不是等它在某个查询上炸开。

判定只认一种情况：`alembic_version` 表存在，但里面的版本号不在当前代码的 head 里。
- 测试库走 `Base.metadata.create_all`，压根没有 `alembic_version` → 跳过，不打扰
- 版本号对得上 → 跳过
其余一律放行，不替数据库猜别的毛病。
"""

import shutil
from datetime import datetime
from functools import lru_cache
from pathlib import Path

from sqlalchemy import inspect, text
from sqlalchemy.engine import Engine

ROOT = Path(__file__).resolve().parents[2]


@lru_cache(maxsize=1)
def script_heads() -> frozenset[str]:
    """当前代码里的迁移 head。读一次就够——进程运行期间代码不会变。"""
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    cfg = Config()          # 同样不传 ini，避免 fileConfig 把 logging 重配掉
    cfg.set_main_option("script_location", str(ROOT / "alembic"))
    return frozenset(ScriptDirectory.from_config(cfg).get_heads())


def db_revisions(engine: Engine) -> set[str] | None:
    """库里记的迁移版本。没有 `alembic_version` 表时返回 None（不是空集）。"""
    try:
        if not inspect(engine).has_table("alembic_version"):
            return None
        with engine.connect() as conn:
            return {r[0] for r in conn.execute(text("select version_num from alembic_version"))}
    except Exception:
        # 连不上库是另一回事，交给正常的错误路径去报
        return None


def engine_of(session_factory) -> Engine | None:
    """从 sessionmaker 取出 engine。

    取不到就返回 None：测试里 `SessionLocal` 被换成 `lambda: session`，没有 `.kw`。
    那种库是 `create_all` 建的、根本没有 `alembic_version`，检查本来也是空转——
    但**不能退而去开一个 session**：测试共用同一个 session，`with` 一包就会回滚掉
    夹具数据（这个坑踩过）。
    """
    return getattr(session_factory, "kw", {}).get("bind")


def migration_gap(engine: Engine | None) -> str | None:
    """库落后于代码时返回该说的话，否则返回 None。"""
    if engine is None:
        return None
    have = db_revisions(engine)
    if have is None:
        return None
    heads = script_heads()
    if not heads or have == set(heads):
        return None
    return (
        f"这个数据库停在旧版本（{'、'.join(sorted(have)) or '未知'}），"
        f"代码已经到 {'、'.join(sorted(heads))} —— 少跑了一次迁移。\n"
        "在项目目录下执行：conda activate tin && alembic upgrade head\n"
        "迁移只新增表和列，不删除、不改写任何已有数据。"
    )


# ---------------------------------------------------------------- 自动迁移

def _sqlite_path(engine: Engine) -> Path | None:
    url = engine.url
    if url.get_backend_name() != "sqlite" or not url.database or url.database == ":memory:":
        return None
    return Path(url.database)


def backup(engine: Engine, keep: int) -> Path | None:
    """迁移前先复制一份库文件。

    自动迁移之所以敢开，前提就是这一步：出了问题有东西可退。SQLite 复制一个文件
    而已，几百 MB 也就一两秒；非 SQLite（真上了 Postgres）不在这里备份——那得靠
    运维的备份策略，假装备份过了比不备份更危险，所以返回 None 让上层说明。
    """
    path = _sqlite_path(engine)
    if path is None or not path.exists():
        return None
    engine.dispose()          # 先放掉连接，避免复制到写了一半的页
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    out = path.with_name(f"{path.stem}.before-migrate-{stamp}{path.suffix}")
    shutil.copy2(path, out)

    olds = sorted(path.parent.glob(f"{path.stem}.before-migrate-*{path.suffix}"))
    for stale in olds[:-keep] if keep > 0 else olds:
        stale.unlink(missing_ok=True)
    return out


def auto_upgrade(engine: Engine, *, keep_backups: int = 5) -> dict:
    """把库升到最新。返回一份说明，供启动日志与出错时的页面使用。

    **失败不抛出。** 启动时迁移失败如果直接让进程退出，得到的是一个起不来的服务和
    一段只在终端里的堆栈；返回失败让应用照常起来、由 503 页面把错误和手动命令一起
    显示出来，人能看见才修得了。
    """
    gap = migration_gap(engine)
    if gap is None:
        return {"ran": False, "ok": True}

    saved = backup(engine, keep_backups)
    try:
        from alembic import command
        from alembic.config import Config

        # **不传 alembic.ini**：env.py 见到 config_file_name 就会调 fileConfig()，
        # 那会整个重配 logging 并**禁用已有的 logger**——uvicorn 的访问日志会从此
        # 一条不出。env.py 真正要的只有下面这两项，其余 ini 配置与升级无关。
        cfg = Config()
        cfg.set_main_option("script_location", str(ROOT / "alembic"))
        cfg.set_main_option("sqlalchemy.url", str(engine.url.render_as_string(hide_password=False)))
        command.upgrade(cfg, "head")
    except Exception as exc:                       # noqa: BLE001 —— 任何失败都要能显示出来
        return {"ran": True, "ok": False, "backup": saved, "error": f"{type(exc).__name__}: {exc}"}
    return {"ran": True, "ok": True, "backup": saved,
            "to": "、".join(sorted(script_heads()))}
