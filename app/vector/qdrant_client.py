"""Thin vector-store wrapper behind an interface (PRD §6.2; IMPLEMENTATION.md Phase 4).

Everything the app does to Qdrant goes through the ``VectorStore`` protocol, so the concrete client
is swappable (the PRD names Chroma as the documented fallback) and — critically — so the
"never write to Qdrant from a request handler" invariant (CLAUDE.md #3) is easy to audit: the only
callers of ``upsert``/``delete``/``clear_all`` are ``app.services.outbox_worker`` and the seed/reset
scripts, never a route. Read-only methods (``all_point_ids``, ``count``, ``retrieve``, ``search``)
are safe to call from anywhere, including ``GET /api/admin/sync-status``.

Collection name / dimension / payload contract are imported from ``app.db.qdrant_bootstrap`` — this
module never re-declares them (orchestrator note). Point id == ``str(product_id)``, which is what
makes an outbox replay idempotent: re-processing a product's row overwrites the same point.
"""

from __future__ import annotations

import uuid
from typing import Any, Iterable, Protocol, runtime_checkable

from qdrant_client import AsyncQdrantClient, models

from app.core.logging import get_logger
from app.db.qdrant_bootstrap import PRODUCTS_COLLECTION, build_qdrant_client

logger = get_logger(__name__)

# Qdrant scroll page size when enumerating every point id for the sync-status audit.
_SCROLL_PAGE = 256

ProductId = uuid.UUID | str


def _point_id(product_id: ProductId) -> str:
    """Canonical Qdrant point id for a product — always the UUID's string form."""
    return str(product_id)


@runtime_checkable
class VectorStore(Protocol):
    """The vector operations the app depends on. Implemented by ``QdrantVectorStore`` below."""

    async def upsert(
        self, product_id: ProductId, vector: list[float], payload: dict[str, Any]
    ) -> None: ...

    async def delete(self, product_id: ProductId) -> None: ...

    async def retrieve(self, product_id: ProductId) -> models.Record | None: ...

    async def retrieve_vectors(
        self, product_ids: Iterable[ProductId]
    ) -> dict[str, list[float]]: ...

    async def all_point_ids(self) -> set[str]: ...

    async def count(self) -> int: ...

    async def search(
        self,
        vector: list[float],
        *,
        limit: int = 12,
        query_filter: models.Filter | None = None,
    ) -> list[models.ScoredPoint]: ...

    async def clear_all(self) -> None: ...

    async def close(self) -> None: ...


class QdrantVectorStore:
    """``VectorStore`` backed by Qdrant's async client, scoped to the ``products`` collection.

    Owns one ``AsyncQdrantClient``. The outbox worker and the scheduler construct one long-lived
    store; ``drain_outbox_once`` builds a throwaway one when none is passed and closes it after.
    """

    def __init__(
        self,
        *,
        client: AsyncQdrantClient | None = None,
        collection: str = PRODUCTS_COLLECTION,
    ) -> None:
        self._client = client or build_qdrant_client()
        self._collection = collection

    @property
    def client(self) -> AsyncQdrantClient:
        return self._client

    async def upsert(
        self, product_id: ProductId, vector: list[float], payload: dict[str, Any]
    ) -> None:
        """Create or overwrite the product's point (id = product_id). Idempotent by construction."""
        await self._client.upsert(
            collection_name=self._collection,
            points=[models.PointStruct(id=_point_id(product_id), vector=vector, payload=payload)],
            wait=True,
        )

    async def delete(self, product_id: ProductId) -> None:
        """Remove the product's point. A no-op if it is already absent (idempotent replay-safe)."""
        await self._client.delete(
            collection_name=self._collection,
            points_selector=models.PointIdsList(points=[_point_id(product_id)]),
            wait=True,
        )

    async def retrieve(self, product_id: ProductId) -> models.Record | None:
        """Fetch one point (payload only, no vector) or ``None`` if it does not exist."""
        records = await self._client.retrieve(
            collection_name=self._collection,
            ids=[_point_id(product_id)],
            with_payload=True,
            with_vectors=False,
        )
        return records[0] if records else None

    async def retrieve_vectors(
        self, product_ids: Iterable[ProductId]
    ) -> dict[str, list[float]]:
        """Fetch the stored vectors for several points at once — ``{point_id: vector}``.

        Read-only and additive (Phase 5 profile centroid): the profile builder averages these
        pre-computed product vectors instead of embedding anything, keeping the ingest/profile path
        LLM-free (invariant #6/#7). Points that don't exist (or carry no vector) are simply absent
        from the returned map. An empty input makes no network call.
        """
        ids = [_point_id(pid) for pid in product_ids]
        if not ids:
            return {}
        records = await self._client.retrieve(
            collection_name=self._collection,
            ids=ids,
            with_payload=False,
            with_vectors=True,
        )
        result: dict[str, list[float]] = {}
        for record in records:
            vector = record.vector
            # This collection uses a single unnamed vector; guard against a named-vector dict anyway.
            if isinstance(vector, dict):
                vector = next(iter(vector.values()), None)
            if vector is not None:
                result[str(record.id)] = list(vector)
        return result

    async def all_point_ids(self) -> set[str]:
        """Every point id in the collection, as strings — the Qdrant side of the sync-status diff."""
        ids: set[str] = set()
        offset: Any = None
        while True:
            points, offset = await self._client.scroll(
                collection_name=self._collection,
                limit=_SCROLL_PAGE,
                with_payload=False,
                with_vectors=False,
                offset=offset,
            )
            ids.update(str(point.id) for point in points)
            if offset is None:
                break
        return ids

    async def count(self) -> int:
        """Exact point count in the collection."""
        result = await self._client.count(collection_name=self._collection, exact=True)
        return result.count

    async def search(
        self,
        vector: list[float],
        *,
        limit: int = 12,
        query_filter: models.Filter | None = None,
    ) -> list[models.ScoredPoint]:
        """Dense similarity search (used by the Phase 6 hybrid retriever). Read-only."""
        return await self._client.search(
            collection_name=self._collection,
            query_vector=vector,
            limit=limit,
            query_filter=query_filter,
            with_payload=True,
        )

    async def clear_all(self) -> None:
        """Delete every point in the collection (seed/reset only — never a request path)."""
        await self._client.delete(
            collection_name=self._collection,
            points_selector=models.FilterSelector(filter=models.Filter()),
            wait=True,
        )

    async def close(self) -> None:
        await self._client.close()
