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


async def run_light_migrations(conn) -> None:
    """Additive, idempotent schema patch for columns/tables added after the
    original deploy.

    `Base.metadata.create_all` (called right before this in main.py's
    lifespan) only creates tables that don't exist yet -- it never alters an
    existing table, so a `users` table already sitting in a live Railway
    Postgres DB would not gain new columns like `pdf_queue_limit` on its
    own. `ADD COLUMN IF NOT EXISTS` is safe to run on every startup and
    covers that gap without pulling in a full Alembic revision for one
    column. Prefer a real Alembic migration for anything more involved than
    this.
    """
    from sqlalchemy import text
    await conn.execute(text(
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS pdf_queue_limit "
        "INTEGER NOT NULL DEFAULT 20"
    ))
    # Centralized per-user limit overrides (utils.limits.FEATURE_LIMITS).
    # All nullable -- NULL means "no personal override, use the settings
    # default" (see utils.limits.get_effective_limits).
    for column in (
        "file_size_limit",
        "batch_limit",
        "archive_compress_limit",
        "archive_extract_return_limit",
        "image_to_pdf_limit",
        "pdf_split_limit",
    ):
        await conn.execute(text(
            f"ALTER TABLE users ADD COLUMN IF NOT EXISTS {column} INTEGER"
        ))
