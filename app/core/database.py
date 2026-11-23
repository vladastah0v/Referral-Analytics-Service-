from collections.abc import AsyncIterator
from functools import lru_cache
from pathlib import Path

from alembic.script import ScriptDirectory
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from app.core.config import get_settings
from app.core.metrics import instrument_database


class Base(DeclarativeBase):
    pass


engine = create_async_engine(get_settings().database_url, pool_pre_ping=True)
instrument_database(engine.sync_engine)
session_factory = async_sessionmaker(engine, expire_on_commit=False)


@lru_cache
def database_schema_head() -> str:
    return ScriptDirectory(
        str(Path(__file__).resolve().parents[2] / "migrations")
    ).get_current_head()


async def get_session() -> AsyncIterator[AsyncSession]:
    async with session_factory() as session:
        yield session
