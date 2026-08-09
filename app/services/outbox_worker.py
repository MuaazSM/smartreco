"""The transactional-outbox drain (PRD §6.2 + Figure 3; CLAUDE.md "The outbox").

The worker is the **only** thing in the codebase that writes to Qdrant. It claims pending
``vector_outbox`` rows with ``SELECT ... FOR UPDATE SKIP LOCKED`` (so N concurrent workers never
double-process a row), applies each op to the vector store, and — in the *same* transaction that
holds the row lock — marks the row ``done`` and stamps ``products.vector_synced_at``. A row whose
Qdrant call raises is retried with exponential backoff and marked ``failed`` after 5 attempts, where
``GET /api/admin/sync-status`` surfaces it to the admin.

Two entry points:
  * ``drain_outbox_once`` — process one batch and return how many rows succeeded. Called directly by
    tests and the seed script (deterministic, no background timer involved).
  * ``scheduled_drain`` — the swallow-all-errors wrapper APScheduler runs every 5s from the app
    lifespan; a drain failure must never kill the recurring job.

Re-embedding is skipped when unchanged: the worker embeds via ``embeddings.embed_product`` keyed by
``content_hash`` (from the outbox payload), so replaying a row for text already embedded this process
is a cache hit, not a Mesh call.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.logging import get_logger
from app.db.models import Product, VectorOutbox
from app.db.session import AsyncSessionLocal
from app.vector import embeddings
from app.vector.qdrant_client import QdrantVectorStore, VectorStore

logger = get_logger(__name__)

_DEFAULT_BATCH = 100
_MAX_ATTEMPTS = 5  # after this many failed tries a row is parked as 'failed' for admin attention
_BACKOFF_CAP_SECONDS = 300.0  # exponential backoff ceiling between retries


def _claim_stmt(limit: int):
    """Pending rows that are due (never tried, or past their exponential-backoff window), locked.

    ``FOR UPDATE SKIP LOCKED`` makes this concurrent-safe: a row another worker already holds is
    skipped rather than blocked on. Backoff is derived from ``attempts`` + ``processed_at`` (the last
    attempt time) so no extra column is needed — never-attempted rows (``processed_at IS NULL``) are
    always immediately claimable, which keeps direct ``drain_outbox_once`` calls deterministic.
    """
    backoff = func.make_interval(
        0, 0, 0, 0, 0, 0, func.least(func.power(2.0, VectorOutbox.attempts), _BACKOFF_CAP_SECONDS)
    )
    return (
        select(VectorOutbox)
        .where(
            VectorOutbox.status == "pending",
            or_(
                VectorOutbox.processed_at.is_(None),
                VectorOutbox.processed_at < func.now() - backoff,
            ),
        )
        .order_by(VectorOutbox.id)
        .limit(limit)
        .with_for_update(skip_locked=True)
    )


async def _apply_upsert(vector_store: VectorStore, row: VectorOutbox) -> None:
    payload: dict[str, Any] = row.payload or {}
    embed_text = payload["embed_text"]
    content_hash = payload.get("content_hash")
    vector_payload = payload["vector_payload"]
    vector = await embeddings.embed_product(embed_text, content_hash=content_hash)
    await vector_store.upsert(row.product_id, vector, vector_payload)


async def _apply_delete(vector_store: VectorStore, row: VectorOutbox) -> None:
    await vector_store.delete(row.product_id)


async def _process_row(session: AsyncSession, vector_store: VectorStore, row: VectorOutbox) -> bool:
    """Apply one outbox op. On success mark it done + stamp the product; on failure back it off.

    Returns True iff the row was applied to the vector store. Never raises — a single row's failure
    must not abort the rest of the claimed batch (their state changes all commit together at the end).
    """
    try:
        if row.op == "upsert":
            await _apply_upsert(vector_store, row)
        elif row.op == "delete":
            await _apply_delete(vector_store, row)
        else:  # guarded by a CHECK constraint, but never trust the row blindly
            raise ValueError(f"unknown outbox op {row.op!r}")
    except Exception as exc:  # noqa: BLE001 - recorded on the row and surfaced via sync-status
        row.attempts += 1
        row.last_error = f"{type(exc).__name__}: {exc}"[:2000]
        row.processed_at = func.now()
        if row.attempts >= _MAX_ATTEMPTS:
            row.status = "failed"
        logger.error(
            "outbox.row_failed",
            extra={
                "extra_fields": {
                    "outbox_id": row.id,
                    "product_id": str(row.product_id),
                    "op": row.op,
                    "attempts": row.attempts,
                    "status": row.status,
                    "error": row.last_error,
                }
            },
        )
        return False

    row.status = "done"
    row.last_error = None
    row.processed_at = func.now()
    await session.execute(
        update(Product).where(Product.id == row.product_id).values(vector_synced_at=func.now())
    )
    return True


async def drain_outbox_once(
    *,
    session_factory: async_sessionmaker[AsyncSession] = AsyncSessionLocal,
    vector_store: VectorStore | None = None,
    limit: int = _DEFAULT_BATCH,
) -> int:
    """Claim and process one batch of pending outbox rows. Returns the number that succeeded.

    The claim + Qdrant I/O + status update all happen inside one transaction, so the row lock is held
    for the whole apply — no other worker can grab a row mid-flight. When ``vector_store`` is omitted
    a throwaway Qdrant store is built and closed within the call (used by tests and the seed script).
    """
    owns_store = vector_store is None
    store: VectorStore = vector_store or QdrantVectorStore()
    processed = 0
    try:
        async with session_factory() as session:
            rows = (await session.execute(_claim_stmt(limit))).scalars().all()
            if not rows:
                return 0
            for row in rows:
                if await _process_row(session, store, row):
                    processed += 1
            await session.commit()
        if processed or rows:
            logger.info(
                "outbox.drained",
                extra={"extra_fields": {"claimed": len(rows), "succeeded": processed}},
            )
        return processed
    finally:
        if owns_store:
            await store.close()


async def scheduled_drain(vector_store: VectorStore) -> None:
    """APScheduler entry point (every 5s). Swallows all errors so the recurring job never dies."""
    try:
        await drain_outbox_once(vector_store=vector_store)
    except Exception as exc:  # noqa: BLE001 - one bad cycle must not stop the scheduler
        logger.error(
            "outbox.drain_cycle_failed",
            extra={"extra_fields": {"detail": f"{type(exc).__name__}: {exc}"}},
        )
