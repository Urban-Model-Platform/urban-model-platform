from logging.config import fileConfig
import os

from sqlalchemy import engine_from_config, pool
from sqlmodel import SQLModel

from alembic import context

# Import ORM table models so that SQLModel.metadata is populated.
# These must be imported before target_metadata is assigned — Alembic's
# autogenerate inspects the metadata at import time.
# Only the adapter-layer ORM models are imported here; the core domain
# models (pure Pydantic) are never touched by Alembic.
import ump.adapters.sqlmodel_job_repository  # noqa: F401  registers JobRecord, JobStatusHistoryRecord

# Alembic Config object
config = context.config

# Override sqlalchemy.url from environment variable when present.
# This allows running `alembic upgrade head` without hard-coding credentials
# in alembic.ini.  UMP_DATABASE_URL must be a *synchronous* DSN for Alembic
# (e.g. postgresql+psycopg2://... or postgresql://...) even though the app
# uses asyncpg at runtime.
db_url = os.environ.get("UMP_DATABASE_URL")
if db_url:
    # Strip async driver prefix so Alembic can use a sync engine
    sync_url = db_url.replace("postgresql+asyncpg://", "postgresql+psycopg2://")
    config.set_main_option("sqlalchemy.url", sync_url)

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
    """Run migrations in 'online' mode.

    In this scenario we need to create an Engine
    and associate a connection with the context.

    """
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection, target_metadata=target_metadata
        )

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
