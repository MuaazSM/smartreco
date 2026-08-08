"""Async SQLAlchemy engine, session factory, and the FastAPI `get_db` dependency.

One process-wide `AsyncEngine` (connection-pooled), one `async_sessionmaker`. Routes depend on
`get_db()`; services receive an `AsyncSession` from the route layer rather than opening their own —
this keeps the transaction boundary (e.g. the product + outbox dual-write in Phase 4) explicit and
callable-once-per-request. No `Base.metadata.create_all()` anywhere — schema exists only via Alembic.
"""

from __future__ import annotations

from typing import AsyncIterator

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import settings

engine: AsyncEngine = create_async_engine(
    settings.database_url,
    pool_pre_ping=True,
    future=True,
)

AsyncSessionLocal: async_sessionmaker[AsyncSession] = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autoflush=False,
)


async def get_db() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency yielding a request-scoped `AsyncSession`.

    Rolls back on any exception raised while the session is in use (so a route that raises
    `HTTPException` mid-transaction never leaves a half-committed write behind), and always closes
    the session, returning its connection to the pool.
    """
    async with AsyncSessionLocal() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()


async def dispose_engine() -> None:
    """Dispose the connection pool. Called from `app.main`'s lifespan on shutdown."""
    await engine.dispose()
