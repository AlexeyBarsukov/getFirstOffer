"""Тесты на настоящем PostgreSQL из docker compose.

Используется отдельная база (по умолчанию getfirstoffer_test), схема создаётся миграциями Alembic.
Каждый тест работает внутри транзакции, которая откатывается в конце.
Если PostgreSQL недоступен, тесты пропускаются.
"""

import asyncio
import os
from collections.abc import AsyncIterator
from pathlib import Path

import asyncpg
import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine

from app.config import get_settings

BACKEND_DIR = Path(__file__).resolve().parents[2]


def resolve_test_database_url() -> str:
    if url := os.environ.get("TEST_DATABASE_URL"):
        return url
    dev_url = make_url(get_settings().database_url)
    return dev_url.set(database=f"{dev_url.database}_test").render_as_string(hide_password=False)


async def recreate_database(url: str) -> None:
    sa_url = make_url(url)
    conn = await asyncpg.connect(
        host=sa_url.host,
        port=sa_url.port,
        user=sa_url.username,
        password=sa_url.password,
        database="postgres",
    )
    try:
        await conn.execute(f'DROP DATABASE IF EXISTS "{sa_url.database}" WITH (FORCE)')
        await conn.execute(f'CREATE DATABASE "{sa_url.database}"')
    finally:
        await conn.close()


def run_migrations(url: str) -> None:
    config = Config(str(BACKEND_DIR / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND_DIR / "migrations"))
    config.set_main_option("sqlalchemy.url", url)
    command.upgrade(config, "head")


@pytest.fixture(scope="session")
async def db_engine() -> AsyncIterator[AsyncEngine]:
    url = resolve_test_database_url()
    try:
        await recreate_database(url)
    except (OSError, asyncpg.PostgresError) as exc:
        pytest.skip(f"PostgreSQL недоступен ({exc!r}). Запусти: docker compose up -d --wait")
    # env.py вызывает asyncio.run, поэтому миграции — в отдельном потоке
    await asyncio.to_thread(run_migrations, url)
    engine = create_async_engine(url)
    yield engine
    await engine.dispose()


@pytest.fixture
async def session(db_engine: AsyncEngine) -> AsyncIterator[AsyncSession]:
    async with db_engine.connect() as conn:
        transaction = await conn.begin()
        # commit внутри теста превращается в savepoint, внешняя транзакция откатывается
        async with AsyncSession(
            bind=conn, expire_on_commit=False, join_transaction_mode="create_savepoint"
        ) as s:
            yield s
        await transaction.rollback()
