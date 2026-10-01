"""Общая обвязка тестов. Тесты НЕ трогают боевую базу.

Тесты с базой работают на отдельной базе из `TEST_DATABASE_URL` и каждый
раз откатывают транзакцию. Если переменная не задана, такие тесты
пропускаются: большая часть проверок — чистые функции.
"""

from __future__ import annotations

import os

import pytest
import pytest_asyncio

TEST_DSN = os.getenv("TEST_DATABASE_URL", "")

# Конфиг читается при импорте приложения, поэтому обязательные переменные
# подставляются до первого импорта app.*.
os.environ.setdefault("BOT_TOKEN", "1:test")
os.environ.setdefault("TZ", "Europe/Moscow")

# ⚠️ DATABASE_URL и APP_DATABASE_URL ПЕРЕБИВАЮТСЯ, а не setdefault: код
# приложения внутри теста (например, alerts._deliver → session_scope)
# открывает свою сессию по этим переменным, и если в окружении (.env) они
# указывают на боевую базу, тест писал бы туда. Тестовая база задана —
# значит, всё ходит только в неё.
if TEST_DSN:
    os.environ["DATABASE_URL"] = TEST_DSN
    os.environ["APP_DATABASE_URL"] = TEST_DSN
else:
    os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test/test")


requires_db = pytest.mark.skipif(
    not TEST_DSN, reason="TEST_DATABASE_URL не задан — тесты с базой пропущены"
)


@pytest_asyncio.fixture
async def session():
    """Сессия в транзакции, которая всегда откатывается.

    Так тест может писать что угодно, не оставляя следов: соседний тест
    видит исходное состояние базы, а порядок запуска ни на что не влияет.
    """
    from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

    from app.db.models import Base

    engine = create_async_engine(TEST_DSN)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)

    connection = await engine.connect()
    transaction = await connection.begin()
    async_session = AsyncSession(bind=connection, expire_on_commit=False)
    try:
        yield async_session
    finally:
        await async_session.close()
        await transaction.rollback()
        await connection.close()
        await engine.dispose()
