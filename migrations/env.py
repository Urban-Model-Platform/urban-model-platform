import asyncio
import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool
from sqlalchemy.ext.asyncio import create_async_engine
from sqlmodel import SQLModel

# Import ORM table models so that SQLModel.metadata is populated.
# These must be imported before target_metadata is assigned — Alembic's
# autogenerate inspects the metadata at import time.
# Only the adapter-layer ORM models are imported here; the core domain
# models (pure Pydantic) are never touched by Alembic.
import ump.adapters.sqlmodel_job_repository  # noqa: F401  registers JobRecord, JobStatusHistoryRecord

# Alembic Config object
config = context.config

# Override sqlalchemy.url from environment variable when present.
# Priority:
#   1. UMP_DATABASE_URL  (full DSN — asyncpg prefix stripped for sync Alembic engine)
#   2. Individual UMP_DATABASE_* vars  (UMP_DATABASE_HOST / PORT / USER / PASSWORD / NAME)
#   3. alembic.ini placeholder (will fail at runtime — useful only for autogenerate dry-runs)
db_url = os.environ.get("UMP_DATABASE_URL")
if db_url:
    # Strip async driver prefix so Alembic can use a sync engine
    sync_url = db_url.replace("postgresql+asyncpg://", "postgresql+psycopg2://")
    config.set_main_option("sqlalchemy.url", sync_url)
else:
    host = os.environ.get("UMP_DATABASE_HOST", "localhost")
    port = os.environ.get("UMP_DATABASE_PORT", "5432")
    user = os.environ.get("UMP_DATABASE_USER", "postgres")
    password = os.environ.get("UMP_DATABASE_PASSWORD", "postgres")
    name = os.environ.get("UMP_DATABASE_NAME", "ump")
    config.set_main_option(
        "sqlalchemy.url",
        f"postgresql+asyncpg://{user}:{password}@{host}:{port}/{name}",
    )

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Use SQLModel's shared metadata so autogenerate detects our table models.
target_metadata = SQLModel.metadata

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
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode using an async engine (asyncpg)."""

    url = config.get_main_option("sqlalchemy.url")

    def do_run_migrations(connection):
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()

    async def run_async_migrations():
        connectable = create_async_engine(url, poolclass=pool.NullPool)
        async with connectable.connect() as connection:
            await connection.run_sync(do_run_migrations)
        await connectable.dispose()

    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
