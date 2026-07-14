"""Async SQLAlchemy engine/session setup."""
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import declarative_base

from core.config import settings

Base = declarative_base()


def _to_asyncpg_url(url: str) -> str:
    """Ensure the DATABASE_URL uses the asyncpg driver, regardless of which
    postgres:// / postgresql:// scheme was supplied (Railway typically
    provides `postgresql://`, which SQLAlchemy's async engine cannot use
    directly).
    """
    if url.startswith("postgresql+asyncpg://"):
        return url
    if url.startswith("postgresql://"):
        return url.replace("postgresql://", "postgresql+asyncpg://", 1)
    if url.startswith("postgres://"):
        return url.replace("postgres://", "postgresql+asyncpg://", 1)
    return url


engine = create_async_engine(
    _to_asyncpg_url(settings.DATABASE_URL),
    echo=False,
    pool_size=10,
    max_overflow=20,
    pool_pre_ping=True,
)

async_session = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
)

# Kept as an alias for backward compatibility with any code referencing the
# old name.
AsyncSessionLocal = async_session


async def get_session() -> AsyncSession:
    """Dependency-style async generator for a DB session."""
    async with async_session() as session:
        yield session
      
