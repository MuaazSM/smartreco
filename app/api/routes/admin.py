"""Admin product CRUD + the store-sync audit (PRD §6.2; IMPLEMENTATION.md Phase 4).

Every route here is guarded by ``require_role("admin")``. The CRUD routes are deliberately thin
wrappers over ``app.services.catalog`` — they do **no** vector I/O themselves (invariant #3); the
catalog service writes the ``products`` row and the ``vector_outbox`` row in one transaction and
returns, and the background worker syncs Qdrant within one drain cycle.

``GET /api/admin/sync-status`` is the single most demonstrable claim in the submission (treat any
failure here as P0). It *reads* both stores — active product ids from Postgres, point ids from Qdrant
— and reports whether they agree, plus how far behind the outbox is. Reading Qdrant from a handler is
fine; the invariant forbids only *writes* from the request path.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import require_role
from app.api.routes.products import ProductOut
from app.core.logging import get_logger
from app.db.models import User, VectorOutbox
from app.db.session import get_db
from app.services import catalog
from app.vector.qdrant_client import QdrantVectorStore

logger = get_logger(__name__)

router = APIRouter(prefix="/api/admin", tags=["admin"])


class ProductCreate(BaseModel):
    title: str = Field(..., min_length=1, max_length=300)
    description: str = Field(default="", max_length=5000)
    category: str = Field(..., min_length=1, max_length=100)
    tags: list[str] = Field(default_factory=list)
    level: str = Field(..., pattern="^(beginner|intermediate|advanced)$")
    price_cents: int = Field(default=0, ge=0)
    is_active: bool = True


class ProductUpdate(BaseModel):
    """All fields optional — only the ones provided are applied (a true PATCH)."""

    title: str | None = Field(default=None, min_length=1, max_length=300)
    description: str | None = Field(default=None, max_length=5000)
    category: str | None = Field(default=None, min_length=1, max_length=100)
    tags: list[str] | None = None
    level: str | None = Field(default=None, pattern="^(beginner|intermediate|advanced)$")
    price_cents: int | None = Field(default=None, ge=0)
    is_active: bool | None = None


class SyncStatus(BaseModel):
    """Postgres-vs-Qdrant reconciliation for the admin console (PRD §6.2)."""

    in_sync: bool
    missing_in_vector: list[str]  # active products with no Qdrant point yet
    orphaned_in_vector: list[str]  # Qdrant points with no active product
    outbox_lag_seconds: float  # age of the oldest still-pending outbox row (0 when caught up)
    pending_count: int
    failed_count: int


@router.post("/products", response_model=ProductOut, status_code=status.HTTP_201_CREATED)
async def create_product(
    body: ProductCreate,
    db: AsyncSession = Depends(get_db),
    _admin: User = Depends(require_role("admin")),
) -> ProductOut:
    """Create a product; the matching ``upsert`` outbox row is written in the same transaction."""
    product = await catalog.create_product(
        db,
        title=body.title,
        description=body.description,
        category=body.category,
        tags=body.tags,
        level=body.level,
        price_cents=body.price_cents,
        is_active=body.is_active,
    )
    return ProductOut.model_validate(product)


@router.patch("/products/{product_id}", response_model=ProductOut)
async def update_product(
    product_id: uuid.UUID,
    body: ProductUpdate,
    db: AsyncSession = Depends(get_db),
    _admin: User = Depends(require_role("admin")),
) -> ProductOut:
    """Apply a partial update; an ``upsert`` outbox row is enqueued only if a synced field changed."""
    provided = body.model_dump(exclude_unset=True)
    try:
        product = await catalog.update_product(db, product_id, **provided)
    except catalog.ProductNotFoundError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Product not found")
    return ProductOut.model_validate(product)


@router.delete("/products/{product_id}", response_model=ProductOut)
async def delete_product(
    product_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    _admin: User = Depends(require_role("admin")),
) -> ProductOut:
    """Soft-delete a product (``is_active=false``) and enqueue a ``delete`` outbox row."""
    try:
        product = await catalog.delete_product(db, product_id)
    except catalog.ProductNotFoundError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Product not found")
    return ProductOut.model_validate(product)


@router.get("/sync-status", response_model=SyncStatus)
async def sync_status(
    db: AsyncSession = Depends(get_db),
    _admin: User = Depends(require_role("admin")),
) -> SyncStatus:
    """Reconcile Postgres active products against Qdrant points (read-only, no vector *write*)."""
    active_ids = await catalog.active_product_ids(db)

    store = QdrantVectorStore()
    try:
        point_ids = await store.all_point_ids()
    finally:
        await store.close()

    missing_in_vector = sorted(active_ids - point_ids)
    orphaned_in_vector = sorted(point_ids - active_ids)

    pending_count = int(
        await db.scalar(
            select(func.count()).select_from(VectorOutbox).where(VectorOutbox.status == "pending")
        )
        or 0
    )
    failed_count = int(
        await db.scalar(
            select(func.count()).select_from(VectorOutbox).where(VectorOutbox.status == "failed")
        )
        or 0
    )
    oldest_pending: datetime | None = await db.scalar(
        select(func.min(VectorOutbox.created_at)).where(VectorOutbox.status == "pending")
    )
    if oldest_pending is not None:
        now: datetime = await db.scalar(select(func.now()))  # type: ignore[assignment]
        outbox_lag_seconds = max(0.0, (now - oldest_pending).total_seconds())
    else:
        outbox_lag_seconds = 0.0

    # "in_sync: true" means fully settled AND fresh: the two stores agree on ids, nothing is parked
    # as failed, AND nothing is still pending. A pending content-update leaves Qdrant holding a stale
    # vector for an id that is in both sets (so no missing/orphaned would catch it) — hence pending
    # must count against in_sync for the claim to be truthful.
    drift = bool(missing_in_vector) or bool(orphaned_in_vector) or failed_count > 0
    result = SyncStatus(
        in_sync=not drift and pending_count == 0,
        missing_in_vector=missing_in_vector,
        orphaned_in_vector=orphaned_in_vector,
        outbox_lag_seconds=outbox_lag_seconds,
        pending_count=pending_count,
        failed_count=failed_count,
    )
    # Only genuine drift is an error worth surfacing. A nonzero pending_count is normal transient
    # "still settling" state that the 5s drain clears — logging it as an error would be a false alarm.
    if drift:
        logger.error(
            "admin.sync_status_degraded",
            extra={
                "extra_fields": {
                    "missing_in_vector": len(missing_in_vector),
                    "orphaned_in_vector": len(orphaned_in_vector),
                    "failed_count": failed_count,
                }
            },
        )
    return result
