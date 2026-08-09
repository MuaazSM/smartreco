"""Product CRUD + the transactional outbox dual-write (PRD §6.2 + Figure 3; CLAUDE.md invariant #3).

This is the submission's core differentiator. Every mutation here writes the ``products`` row **and**
its matching ``vector_outbox`` row inside **one** transaction — both commit or neither does. There is
**no** Qdrant call anywhere in this module: the request path only ever touches Postgres, and the
worker (``app.services.outbox_worker``) is the sole thing that talks to the vector store. That is what
lets ``GET /api/admin/sync-status`` make its "the two stores are provably in sync" claim.

Design of the three mutations:

  * **create** — insert the product (computing ``content_hash`` from its embeddable text) and enqueue
    one ``upsert`` outbox row carrying everything the worker needs to embed + upsert the point.
  * **update** — recompute ``content_hash``; enqueue an ``upsert`` row **only if** something the vector
    store cares about actually changed (embeddable content, or the ``price_cents``/``is_active``
    payload metadata). A no-op update touches nothing and enqueues nothing — so it can never trigger a
    re-embed (PRD §6.2 "skip re-embed when unchanged").
  * **delete** — soft-delete (``is_active = false``) and enqueue a ``delete`` row; the worker removes
    the point. Rows are never hard-deleted.

The outbox ``payload`` is self-contained: the worker embeds ``embed_text`` (keyed by ``content_hash``,
so identical text is a cache hit, not a Mesh call) and upserts ``vector_payload`` as the point payload.
"""

from __future__ import annotations

import uuid
from typing import Any, Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.db.models import Product, VectorOutbox
from app.vector import embeddings

logger = get_logger(__name__)

# Sentinel so update_product can distinguish "field omitted" from "field set to None/empty".
_UNSET: Any = object()


class ProductNotFoundError(Exception):
    """No active/inactive product exists for the given id (translated to 404 at the route boundary)."""

    def __init__(self, product_id: uuid.UUID | str) -> None:
        super().__init__(f"product {product_id} not found")
        self.product_id = product_id


def _vector_payload(product: Product) -> dict[str, Any]:
    """The Qdrant point payload for a product (PRD §5 payload contract + content_hash for auditing)."""
    return {
        "product_id": str(product.id),
        "title": product.title,
        "category": product.category,
        "tags": list(product.tags),
        "level": product.level,
        "price_cents": product.price_cents,
        "is_active": product.is_active,
        "content_hash": product.content_hash,
    }


def _upsert_outbox_payload(product: Product, embed_text: str) -> dict[str, Any]:
    """Everything the worker needs to embed + upsert this product's point — no DB re-read required."""
    return {
        "product_id": str(product.id),
        "content_hash": product.content_hash,
        "embed_text": embed_text,
        "vector_payload": _vector_payload(product),
    }


async def create_product(
    session: AsyncSession,
    *,
    title: str,
    description: str = "",
    category: str,
    tags: Sequence[str] | None = None,
    level: str,
    price_cents: int = 0,
    is_active: bool = True,
) -> Product:
    """Create a product and enqueue its ``upsert`` outbox row in one transaction.

    Returns fast with **no** vector call. The worker drains the outbox and writes Qdrant within one
    drain cycle (≤5s), after which ``vector_synced_at`` is set and sync-status reports ``in_sync``.
    """
    tag_list = list(tags or [])
    embed_text = embeddings.embeddable_text(
        title=title, description=description, category=category, tags=tag_list, level=level
    )
    content_hash = embeddings.content_hash_for(embed_text)

    product = Product(
        title=title,
        description=description,
        category=category,
        tags=tag_list,
        level=level,
        price_cents=price_cents,
        is_active=is_active,
        content_hash=content_hash,
    )
    session.add(product)
    await session.flush()  # assign the server-side UUID pk so the outbox row can reference it

    outbox = VectorOutbox(
        product_id=product.id,
        op="upsert",
        payload=_upsert_outbox_payload(product, embed_text),
        status="pending",
    )
    session.add(outbox)

    await session.commit()  # product + outbox commit together — the whole point (invariant #3)
    await session.refresh(product)
    logger.info(
        "catalog.create",
        extra={"extra_fields": {"product_id": str(product.id), "outbox_op": "upsert"}},
    )
    return product


async def update_product(
    session: AsyncSession,
    product_id: uuid.UUID | str,
    *,
    title: str = _UNSET,
    description: str = _UNSET,
    category: str = _UNSET,
    tags: Sequence[str] = _UNSET,
    level: str = _UNSET,
    price_cents: int = _UNSET,
    is_active: bool = _UNSET,
) -> Product:
    """Apply the provided fields, recompute ``content_hash``, and enqueue the outbox op that keeps
    Qdrant consistent with the invariant "a point exists iff the product is active".

    An ``is_active`` transition dominates: active→inactive enqueues a ``delete`` (the point is removed,
    exactly as ``delete_product`` does — so deactivating via PATCH can never leave an orphaned point);
    inactive→active enqueues an ``upsert`` to re-add it. While the product stays active, an ``upsert``
    is enqueued only if the embeddable content changed (→ re-embed on drain) or ``price_cents`` moved
    (payload refresh; re-embed skipped via the worker's ``content_hash`` cache). While it stays
    inactive there is no point, so nothing is enqueued. A no-op update enqueues nothing.
    """
    product = await session.get(Product, _as_uuid(product_id))
    if product is None:
        raise ProductNotFoundError(product_id)

    prev_hash = product.content_hash
    prev_price = product.price_cents
    prev_active = product.is_active

    if title is not _UNSET:
        product.title = title
    if description is not _UNSET:
        product.description = description
    if category is not _UNSET:
        product.category = category
    if tags is not _UNSET:
        product.tags = list(tags)
    if level is not _UNSET:
        product.level = level
    if price_cents is not _UNSET:
        product.price_cents = price_cents
    if is_active is not _UNSET:
        product.is_active = is_active

    embed_text = embeddings.embeddable_text(
        title=product.title,
        description=product.description,
        category=product.category,
        tags=product.tags,
        level=product.level,
    )
    new_hash = embeddings.content_hash_for(embed_text)

    content_changed = new_hash != prev_hash
    if content_changed:
        # Keep content_hash current even when deactivating, so a later reactivation compares cleanly.
        product.content_hash = new_hash

    # Pick the outbox op that preserves "a Qdrant point exists iff the product is active". An
    # is_active transition dominates: True->False must DELETE the point (not upsert an inactive one,
    # which would linger forever as an orphan and break sync-status), False->True re-adds it. While
    # the product stays active, sync only when the point's vector/payload actually changed; while it
    # stays inactive there is no point, so nothing needs syncing.
    now_active = product.is_active
    op: str | None = None
    if prev_active and not now_active:
        op = "delete"
    elif not prev_active and now_active:
        op = "upsert"
    elif now_active and (content_changed or product.price_cents != prev_price):
        op = "upsert"

    if op == "upsert":
        session.add(
            VectorOutbox(
                product_id=product.id,
                op="upsert",
                payload=_upsert_outbox_payload(product, embed_text),
                status="pending",
            )
        )
    elif op == "delete":
        session.add(
            VectorOutbox(
                product_id=product.id,
                op="delete",
                payload={"product_id": str(product.id)},
                status="pending",
            )
        )

    await session.commit()
    await session.refresh(product)
    logger.info(
        "catalog.update",
        extra={
            "extra_fields": {
                "product_id": str(product.id),
                "content_changed": content_changed,
                "enqueued_op": op,
            }
        },
    )
    return product


async def delete_product(session: AsyncSession, product_id: uuid.UUID | str) -> Product:
    """Soft-delete a product (``is_active = false``) and enqueue a ``delete`` outbox row in one tx.

    Idempotent: deleting an already-inactive product still enqueues a ``delete`` (harmless — the
    worker's Qdrant delete is a no-op on an absent point), so a retried delete never leaves a stale
    point behind.
    """
    product = await session.get(Product, _as_uuid(product_id))
    if product is None:
        raise ProductNotFoundError(product_id)

    product.is_active = False
    outbox = VectorOutbox(
        product_id=product.id,
        op="delete",
        payload={"product_id": str(product.id)},
        status="pending",
    )
    session.add(outbox)

    await session.commit()
    await session.refresh(product)
    logger.info(
        "catalog.delete",
        extra={"extra_fields": {"product_id": str(product.id), "outbox_op": "delete"}},
    )
    return product


async def get_product(session: AsyncSession, product_id: uuid.UUID | str) -> Product | None:
    """Fetch a product by id regardless of active state (admin/route helper)."""
    return await session.get(Product, _as_uuid(product_id))


async def active_product_ids(session: AsyncSession) -> set[str]:
    """Ids of every active product, as strings — the Postgres side of the sync-status diff."""
    rows = await session.execute(select(Product.id).where(Product.is_active.is_(True)))
    return {str(row[0]) for row in rows.all()}


def _as_uuid(product_id: uuid.UUID | str) -> uuid.UUID:
    return product_id if isinstance(product_id, uuid.UUID) else uuid.UUID(str(product_id))
