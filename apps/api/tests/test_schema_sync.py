"""Schema drift guard.

The API and the trading engine import the same ORM models, but the database is
created by Alembic. If a model is added or changed without a matching migration,
the running app would fail on a column that does not exist. These tests compare
the models against a database built purely by ``alembic upgrade head``.
"""

from __future__ import annotations

import os

import asyncpg
import pytest

_MIGRATION_DB = "vantage_migrated_test"
_ADMIN = os.environ.get("VANTAGE_TEST_DB_ADMIN", "postgresql://vantage:vantage@127.0.0.1:5432")


def _dsn(db: str) -> str:
    return _ADMIN + "/" + db


@pytest.mark.asyncio
async def test_alembic_head_matches_the_orm_models():
    """A fresh database built by Alembic must match the ORM metadata."""

    from trading_engine.persistence.models import Base

    # Create and migrate a scratch database.
    conn = await asyncpg.connect(_dsn("postgres"))
    try:
        await conn.execute(f'DROP DATABASE IF EXISTS "{_MIGRATION_DB}" WITH (FORCE)')
        await conn.execute(f'CREATE DATABASE "{_MIGRATION_DB}"')
    finally:
        await conn.close()

    sync_url = _dsn(_MIGRATION_DB)
    # _ADMIN already carries the postgresql:// scheme; Alembic needs asyncpg.
    async_url = sync_url.replace("postgresql://", "postgresql+asyncpg://", 1)
    env = os.environ.copy()
    env["DATABASE_URL"] = async_url
    env["PGPASSWORD"] = sync_url.rsplit(":", 1)[-1]

    # Run the real migration chain against it.
    import subprocess
    import sys
    from pathlib import Path

    api_root = Path(__file__).resolve().parents[1]
    proc = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=api_root, env=env, capture_output=True, text=True,
    )
    assert proc.returncode == 0, f"alembic upgrade failed:\n{proc.stdout}\n{proc.stderr}"

    # Compare the migrated schema against the models (asyncpg only is available).
    from sqlalchemy.ext.asyncio import create_async_engine

    engine = create_async_engine(async_url)
    async with engine.connect() as conn:
        result = await conn.exec_driver_sql(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = 'vantage'"
        )
        db_tables = {row[0] for row in result}
    await engine.dispose()

    model_tables = {t.name for t in Base.metadata.tables.values()
                    if t.schema in (None, "vantage")}

    # alembic_version is Alembic bookkeeping, not a domain table.
    db_tables.discard("alembic_version")

    missing = model_tables - db_tables
    assert not missing, f"tables in models but not created by the migration: {sorted(missing)}"

    extra = db_tables - model_tables
    assert not extra, f"tables created by the migration but absent from models: {sorted(extra)}"

    # Clean up the scratch database.
    conn = await asyncpg.connect(_dsn("postgres"))
    try:
        await conn.execute(f'DROP DATABASE IF EXISTS "{_MIGRATION_DB}" WITH (FORCE)')
    finally:
        await conn.close()
