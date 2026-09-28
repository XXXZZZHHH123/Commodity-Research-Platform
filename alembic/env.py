from logging.config import fileConfig


from alembic import context

# this is the Alembic Config object, which provides
# access to the values within the .ini file in use.
config = context.config

# Interpret the config file for Python logging.
# This line sets up loggers basically.
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# add your model's MetaData object here
# for 'autogenerate' support
# from myapp import mymodel
# target_metadata = mymodel.Base.metadata
import tin.models  # noqa: F401  注册全部表
from tin.db import Base, make_engine
from tin.config import settings

target_metadata = Base.metadata

# other values from the config, defined by the needs of env.py,
# can be acquired:
# my_important_option = config.get_main_option("my_important_option")
# ... etc.


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode.

    This configures the context with just a URL
    and not an Engine, though an Engine is acceptable
    here as well.  By skipping the Engine creation
    we don't even need a DBAPI to be available.

    Calls to context.execute() here emit the given string to the
    script output.

    """
    context.configure(
        url=settings.database_url,
        render_as_batch=True,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode.

    In this scenario we need to create an Engine
    and associate a connection with the context.

    """
    # 调用方显式给了 url 就用它，否则回落到 settings。
    #
    # 原来无条件 `make_engine()`，等于把传进来的 sqlalchemy.url 丢掉、永远升级
    # settings.database_url 指向的那个库——程序化调用（tin.schema_check.auto_upgrade）
    # 想升别的库时会**静默升错对象**。命令行走 alembic.ini 时这一项是空的，行为不变。
    url = config.get_main_option("sqlalchemy.url", None)
    connectable = make_engine(url) if url else make_engine()

    with connectable.connect() as connection:
        context.configure(
            connection=connection, target_metadata=target_metadata, render_as_batch=True
        )

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
