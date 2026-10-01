"""Подключение к базе."""

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import get_settings

_settings = get_settings()

engine = create_async_engine(
    _settings.runtime_database_url(),
    echo=False,
    pool_pre_ping=True,
    pool_size=5,
    max_overflow=10,
)

SessionMaker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


@asynccontextmanager
async def session_scope() -> AsyncGenerator[AsyncSession, None]:
    async with SessionMaker() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
