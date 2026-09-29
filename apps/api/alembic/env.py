"""Alembic environment.

The database URL and the target metadata both come from the shared engine
package, so migrations can never drift from the models.
"""

from __future__ import annotations

import sys
from logging.config import fileConfig
from pathlib import Path

from alembic import context
import sqlalchemy as sa
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config
from sqlalchemy.pool import NullPool

# Make both the API package and the shared trading engine importable.
API_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = API_ROOT.parents[1]
for p in (str(API_ROOT), str(REPO_ROOT / "apps" / "trading-engine")):
    if p not in sys.path:
        sys.path.insert(0, p)

from app.core.config import get_settings  # noqa: E402
from trading_engine.persistence.models import SCHEMA, Base  # noqa: E402

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

settings = get_settings()
config.set_main_option("sqlalchemy.url", settings.database_url)

target_metadata = Base.metadata


def include_object(obj, name, type_, reflected, compare_to) -> bool:
    # Keep everything inside the vantage schema; never touch other schemas.
    schema = getattr(obj, "schema", None)
    if type_ == "table" and schema not in (SCHEMA, None):
        return False
    return True


def _do_run_migrations(connection: Connection) -> None:
    # The schema must exist before any table can be created inside it. Under
    # SQLAlchemy 2.0 an execute() outside an explicit transaction opens an
    # implicit transaction that is discarded when the connection closes, so this
    # is committed on its own before Alembic takes over.
    connection.execute(sa.text(f'CREATE SCHEMA IF NOT EXISTS "{SCHEMA}"'))
    connection.commit()
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        include_object=include_object,
        compare_type=True,
        compare_server_default=True,
        version_table_schema=SCHEMA,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_offline() -> None:
    context.configure(
        url=settings.database_url,
        target_metadata=target_metadata,
        include_object=include_object,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        version_table_schema=SCHEMA,
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=NullPool,
    )
    async with connectable.connect() as connection:
        await connection.run_sync(_do_run_migrations)
        await connection.commit()
    await connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    import asyncio

    asyncio.run(run_migrations_online())
