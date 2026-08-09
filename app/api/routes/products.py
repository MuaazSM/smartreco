"""Public product catalog reads (PRD §6.2, §8): list with filters + fetch one.

Read-only and unauthenticated — browsing the catalog does not require a login (the tracker in Phase 5
records browse events for logged-in users, but the listing itself is public). Only **active** products
are ever returned here; admins see inactive rows through the admin router. No Qdrant call: catalog
browse is a plain Postgres query (semantic search over the vector store is the agent's job, Phase 6).
"""

from __future__ import annotations

import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Product
from app.db.session import get_db

router = APIRouter(prefix="/api/products", tags=["products"])


class ProductOut(BaseModel):
    """Public product shape returned by every catalog + admin product route."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    title: str
    description: str
    category: str
    tags: list[str]
    level: str
    price_cents: int
    is_active: bool
    vector_synced_at: datetime | None
    created_at: datetime
    updated_at: datetime


class ProductPage(BaseModel):
    """A page of catalog results plus the paging metadata the frontend grid needs."""

    items: list[ProductOut]
    total: int
    page: int
    page_size: int


@router.get("", response_model=ProductPage)
async def list_products(
    db: AsyncSession = Depends(get_db),
    category: str | None = Query(default=None),
    level: str | None = Query(default=None),
    q: str | None = Query(default=None, description="case-insensitive match on title/description"),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
) -> ProductPage:
    """List active products, filtered by ``category``/``level``/``q`` and paginated."""
    filters = [Product.is_active.is_(True)]
    if category:
        filters.append(Product.category == category)
    if level:
        filters.append(Product.level == level)
    if q:
        pattern = f"%{q}%"
        filters.append(or_(Product.title.ilike(pattern), Product.description.ilike(pattern)))

    total = await db.scalar(select(func.count()).select_from(Product).where(*filters)) or 0
    rows = await db.execute(
        select(Product)
        .where(*filters)
        .order_by(Product.created_at.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    items = [ProductOut.model_validate(row) for row in rows.scalars().all()]
    return ProductPage(items=items, total=total, page=page, page_size=page_size)


@router.get("/{product_id}", response_model=ProductOut)
async def get_product(product_id: uuid.UUID, db: AsyncSession = Depends(get_db)) -> Product:
    """Fetch a single active product, or 404 if it is missing or has been soft-deleted."""
    product = await db.get(Product, product_id)
    if product is None or not product.is_active:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Product not found")
    return product
