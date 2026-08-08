"""Idempotent Qdrant collection bootstrap (PRD §5 "Key DDL details", IMPLEMENTATION.md Phase 1).

Creates the `products` collection — cosine distance, 1536-dim (Mesh's `text-embedding-3-small`,
per PRD §6.5's embeddings row) — if it doesn't already exist. Point ID equals `products.id` (a
UUID), which is what makes the Phase 4 outbox worker's upsert naturally idempotent: replaying the
same product's outbox row twice overwrites the same point rather than creating a duplicate.

This module only *declares the collection shape*; it never reads/writes points. Point CRUD lives in
`app/vector/qdrant_client.py` (Phase 4) and `app/services/outbox_worker.py` (Phase 4) — this file is
intentionally kept separate so Phase 4's vector-client work doesn't collide with Phase 1's schema
bootstrap.

Call `bootstrap_qdrant()` once from `app.main`'s lifespan on startup. Safe to call any number of
times, including concurrently from multiple app instances — `collection_exists` + `create_collection`
both handle the "someone else just created it" race by simply leaving the existing collection alone
(Qdrant's `create_collection` on an existing name is also safe: point at `collection_exists` first
so we can log create-vs-skip distinctly).
"""

from __future__ import annotations

from qdrant_client import AsyncQdrantClient, models

from app.core.config import settings
from app.core.logging import get_logger

logger = get_logger(__name__)

PRODUCTS_COLLECTION = "products"
EMBEDDING_DIM = 1536  # openai/text-embedding-3-small via Mesh — see PRD §6.5

# Documents the payload contract every Qdrant point in `products` must carry (PRD §5). Point ID is
# `products.id`, carried implicitly by Qdrant's point id rather than duplicated into the payload,
# but `product_id` is still included per the PRD's literal payload spec so filters/joins can select
# on it without special-casing the point id field.
PRODUCT_PAYLOAD_FIELDS = (
    "product_id",
    "title",
    "category",
    "tags",
    "level",
    "price_cents",
    "is_active",
)


def build_qdrant_client() -> AsyncQdrantClient:
    """Construct a fresh `AsyncQdrantClient` from settings. Caller owns closing it."""
    return AsyncQdrantClient(url=settings.qdrant_url, api_key=settings.qdrant_api_key)


async def ensure_products_collection(
    client: AsyncQdrantClient,
    *,
    collection_name: str = PRODUCTS_COLLECTION,
    vector_size: int = EMBEDDING_DIM,
) -> bool:
    """Create `collection_name` (cosine, `vector_size`-dim) if it doesn't already exist.

    Returns `True` if the collection was created by this call, `False` if it already existed.
    Idempotent and safe to call repeatedly, including back-to-back with no state change in between.
    """
    if await client.collection_exists(collection_name):
        return False

    await client.create_collection(
        collection_name=collection_name,
        vectors_config=models.VectorParams(size=vector_size, distance=models.Distance.COSINE),
    )
    return True


async def bootstrap_qdrant() -> None:
    """Ensure every Qdrant collection SmartReco needs exists. Called once from `app.main` startup.

    Owns its own client (built and closed within this call) so it has no lifecycle coupling to any
    client another module might hold — Phase 4's `app/vector/qdrant_client.py` constructs its own
    client for the request/worker path.
    """
    client = build_qdrant_client()
    try:
        created = await ensure_products_collection(client)
        logger.info(
            "qdrant.bootstrap",
            extra={
                "extra_fields": {
                    "collection": PRODUCTS_COLLECTION,
                    "created": created,
                    "vector_size": EMBEDDING_DIM,
                }
            },
        )
    finally:
        await client.close()
