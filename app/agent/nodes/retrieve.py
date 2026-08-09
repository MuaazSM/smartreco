"""Node 3 — ``retrieve`` (NO LLM). PRD §6.4: dense + BM25 → RRF → filter → MMR(λ=0.7) → top-12.

This node owns the retrieval I/O and hands the data to the pure ``hybrid_retrieve`` core:

  * **query embeddings** — the planned queries are embedded through the single Mesh gateway
    (``mesh.embed`` → ``/v1/embeddings``, the sanctioned embedding path; invariant #7). "No LLM" means
    no chat/generation call here — an embedding for the dense query vector is explicitly allowed.
  * **native Qdrant search** — for the interest centroid and each query vector, a real ``store.search``
    with a Qdrant-side metadata filter runs, and its ranking is injected into the fusion, so the vector
    DB's own filtered ANN genuinely participates (RAG over the vector DB, PRD §6.4 node 3).
  * **product vectors** — fetched once from Qdrant for the in-process dense leg and MMR diversity.

Runs on every refine loop (the graph loops ``refine_queries → retrieve``); ``ctx`` caches the product
vectors so the loop never re-fetches them. Makes no chat/generation call (the cheap backbone).
"""

from __future__ import annotations

from typing import Any

from app.agent.context import dict_to_filters, get_ctx
from app.core.logging import get_logger
from app.llm import mesh
from app.vector import reranker
from app.vector.hybrid_retriever import build_qdrant_filter, hybrid_retrieve

logger = get_logger(__name__)

_SEARCH_LIMIT = 30


async def _native_search_lists(ctx: Any, vectors: list[list[float]], qfilter: Any) -> list[list[str]]:
    """Run Qdrant's native filtered ANN for each dense vector; return the id rankings (best-effort)."""
    lists: list[list[str]] = []
    for vector in vectors:
        if not vector:
            continue
        try:
            points = await ctx.vector_store.search(
                vector, limit=_SEARCH_LIMIT, query_filter=qfilter
            )
        except Exception as exc:  # pragma: no cover - degrade to in-process legs on Qdrant hiccup
            logger.warning("agent.qdrant_search_failed", extra={"extra_fields": {"error": str(exc)}})
            break
        lists.append([str(point.id) for point in points])
    return lists


async def retrieve(state: dict[str, Any], config: Any) -> dict[str, Any]:
    """Hybrid retrieval → top-12 grounding candidates. Embedding only; no chat/generation call."""
    ctx = get_ctx(config)
    queries = state.get("queries") or []
    filters = dict_to_filters(state.get("filters") or {})

    query_vectors: list[list[float]] = []
    if queries:
        embedding = await mesh.embed(queries, on_usage=ctx.on_usage)
        query_vectors = embedding.vectors

    doc_vectors = await ctx.doc_vectors()

    qfilter = build_qdrant_filter(filters, ctx.seen_product_ids)
    dense_vectors = ([ctx.interest_vector] if ctx.interest_vector else []) + query_vectors
    extra_ranked_lists = await _native_search_lists(ctx, dense_vectors, qfilter)

    candidates = hybrid_retrieve(
        queries=queries,
        query_vectors=query_vectors,
        centroid=ctx.interest_vector or None,
        corpus=ctx.corpus,
        doc_vectors=doc_vectors,
        filters=filters,
        exclude_ids=set(ctx.seen_product_ids),
        extra_ranked_lists=extra_ranked_lists,
    )

    if ctx.enable_rerank:
        candidates = await reranker.rerank(
            queries, candidates, use_llm=True, on_usage=ctx.on_usage
        )

    return {"candidates": [cand.as_dict() for cand in candidates], "node_path": ["retrieve"]}
