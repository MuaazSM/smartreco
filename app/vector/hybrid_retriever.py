"""Hybrid retrieval for the agent's ``retrieve`` node (PRD §6.4 node 3; IMPLEMENTATION.md Phase 6).

Per the PRD/Figure 5: for the planned queries do **dense vector search + BM25**, fuse the ranked
lists with **Reciprocal Rank Fusion**, apply the metadata filter, then **MMR at λ=0.7** for diversity
→ **top-12** candidates. This module is the pure, deterministic core of that pipeline; the ``retrieve``
node supplies the I/O (Qdrant reads via ``QdrantVectorStore``, query embeddings via the single Mesh
gateway) and hands the data in here.

Design notes:
  * **No AI client is constructed or imported here** (invariants #1/#7). ``rank_bm25`` is a lexical
    ranker, not a model. Dense scoring is plain cosine over vectors the caller already fetched from
    Qdrant — the vectors *live in* the vector DB; this ranks them.
  * Dense is computed in-process over the fetched product vectors (the catalog is ~40 items) so the
    fusion is a single pure function that is trivially unit-testable; the ``retrieve`` node *also*
    issues a native ``store.search`` (ANN + Qdrant-side metadata filter) and injects its ranking via
    ``extra_ranked_lists`` so the vector DB's own filtered search genuinely participates in the fusion.
  * Cosine is dimension-safe: mismatched-dim vectors contribute 0.0 rather than raising, so an offline
    pseudo-vector (query) never corrupts fusion against real product vectors, and vice versa.
  * The metadata filter self-relaxes (drop price → level → category, never the already-seen exclusion)
    when it would starve the candidate pool, so ``generate``/fallback always have ≥3 grounded options.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Any, Iterable

from qdrant_client import models
from rank_bm25 import BM25Okapi

from app.core.logging import get_logger

logger = get_logger(__name__)

_RRF_K = 60          # Reciprocal Rank Fusion constant (standard default)
_FETCH_K = 30        # how deep each ranked list goes before fusion
_TOP_K = 12          # final candidate count handed to grade/generate (PRD: top-12)
_MMR_LAMBDA = 0.7    # relevance/diversity trade-off (PRD: λ=0.7)
_MIN_POOL = 8        # relax filters until at least this many survive (or filters exhausted)

_TOKEN_RE = re.compile(r"[a-z0-9]+")


# --------------------------------------------------------------------------------------------------
# Data structures
# --------------------------------------------------------------------------------------------------
@dataclass(slots=True)
class ProductDoc:
    """An active catalog row as the retriever/grounding gate sees it (loaded from Postgres)."""

    product_id: str
    title: str
    description: str
    category: str
    tags: list[str]
    level: str
    price_cents: int

    @property
    def search_text(self) -> str:
        """The lexical field BM25 tokenizes — title + description + category + level + tags."""
        return f"{self.title} {self.description} {self.category} {self.level} {' '.join(self.tags)}"


@dataclass(slots=True)
class RetrievalFilters:
    """Normalized metadata filter (a ``PlanFilters`` widened into a category set)."""

    categories: list[str] | None = None
    level: str | None = None
    max_price_cents: int | None = None


@dataclass(slots=True)
class Candidate:
    """A fused, MMR-ordered retrieval candidate. ``score`` is the fused RRF score (for ordering)."""

    product_id: str
    title: str
    category: str
    level: str
    price_cents: int
    tags: list[str] = field(default_factory=list)
    score: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "product_id": self.product_id,
            "title": self.title,
            "category": self.category,
            "level": self.level,
            "price_cents": self.price_cents,
            "tags": list(self.tags),
            "score": round(self.score, 6),
        }


# --------------------------------------------------------------------------------------------------
# Math helpers (dimension-safe)
# --------------------------------------------------------------------------------------------------
def _tokenize(text: str) -> list[str]:
    return _TOKEN_RE.findall(text.lower())


def cosine(a: list[float] | None, b: list[float] | None) -> float:
    """Cosine similarity in ``[-1, 1]``; ``0.0`` for missing/empty/zero-norm/mismatched-dim inputs."""
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = 0.0
    na = 0.0
    nb = 0.0
    for x, y in zip(a, b):
        dot += x * y
        na += x * x
        nb += y * y
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (math.sqrt(na) * math.sqrt(nb))


def _dense_ranking(
    query_vector: list[float], corpus_ids: list[str], doc_vectors: dict[str, list[float]]
) -> list[str]:
    """Rank corpus ids by cosine to ``query_vector`` (descending); ids without a vector are dropped."""
    scored = [
        (pid, cosine(query_vector, doc_vectors.get(pid)))
        for pid in corpus_ids
        if pid in doc_vectors
    ]
    scored = [pair for pair in scored if pair[1] > 0.0]
    scored.sort(key=lambda pair: pair[1], reverse=True)
    return [pid for pid, _ in scored[:_FETCH_K]]


def _bm25_ranking(query: str, corpus: list[ProductDoc]) -> list[str]:
    """Rank corpus ids by BM25 relevance to ``query`` (lexical leg). Empty query → empty list."""
    tokens = _tokenize(query)
    if not tokens or not corpus:
        return []
    bm25 = BM25Okapi([_tokenize(doc.search_text) for doc in corpus])
    scores = bm25.get_scores(tokens)
    ranked = sorted(zip(corpus, scores), key=lambda pair: pair[1], reverse=True)
    return [doc.product_id for doc, score in ranked[:_FETCH_K] if score > 0.0]


def _reciprocal_rank_fusion(ranked_lists: list[list[str]], k: int = _RRF_K) -> dict[str, float]:
    """Fuse several best-first ranked lists into one score map: ``sum 1 / (k + rank)``."""
    scores: dict[str, float] = {}
    for ranking in ranked_lists:
        for rank, pid in enumerate(ranking):
            scores[pid] = scores.get(pid, 0.0) + 1.0 / (k + rank + 1)
    return scores


def _mmr(
    ordered_ids: list[str],
    rrf_scores: dict[str, float],
    doc_vectors: dict[str, list[float]],
    *,
    lambda_: float = _MMR_LAMBDA,
    top_k: int = _TOP_K,
) -> list[str]:
    """Maximal Marginal Relevance re-rank: ``λ·relevance − (1−λ)·max_sim_to_selected``.

    Relevance is the (normalized) RRF score, so the lexical + dense + native-Qdrant signal all feed
    it; diversity is cosine between product vectors. Robust when vectors are missing (diversity → 0).
    """
    if not ordered_ids:
        return []
    top = max(rrf_scores.values()) if rrf_scores else 1.0
    top = top or 1.0
    selected: list[str] = []
    remaining = list(ordered_ids)
    while remaining and len(selected) < top_k:
        best_id: str | None = None
        best_score = -math.inf
        for pid in remaining:
            relevance = rrf_scores.get(pid, 0.0) / top
            if selected:
                diversity = max(
                    cosine(doc_vectors.get(pid), doc_vectors.get(sid)) for sid in selected
                )
            else:
                diversity = 0.0
            score = lambda_ * relevance - (1.0 - lambda_) * diversity
            if score > best_score:
                best_score = score
                best_id = pid
        if best_id is None:
            break
        selected.append(best_id)
        remaining.remove(best_id)
    return selected


# --------------------------------------------------------------------------------------------------
# Metadata filtering (Qdrant-native + Python predicate, same semantics; self-relaxing)
# --------------------------------------------------------------------------------------------------
def build_qdrant_filter(
    filters: RetrievalFilters,
    exclude_ids: Iterable[str],
    *,
    include_payload_filters: bool = False,
) -> models.Filter | None:
    """Build the Qdrant ``Filter`` for the native dense-search leg. Returns ``None`` if unconstrained.

    By default this emits **only** the already-seen point-id exclusion — id-based filtering needs no
    payload index and always works. Category/level/price/active filtering is enforced in-process by the
    Python predicate (``_filtered_ids``) instead, because it must apply uniformly to the BM25 leg too
    and the shared Qdrant collection carries no payload indexes (a payload-field filter would 400 with
    "Index required but not found"). Set ``include_payload_filters=True`` only against a collection that
    has indexed ``is_active``/``category``/``level``/``price_cents`` — then the same predicate is pushed
    down to Qdrant as well.
    """
    must: list[models.Condition] = []
    if include_payload_filters:
        must.append(models.FieldCondition(key="is_active", match=models.MatchValue(value=True)))
        if filters.categories:
            must.append(
                models.FieldCondition(
                    key="category", match=models.MatchAny(any=list(filters.categories))
                )
            )
        if filters.level:
            must.append(
                models.FieldCondition(key="level", match=models.MatchValue(value=filters.level))
            )
        if filters.max_price_cents is not None:
            must.append(
                models.FieldCondition(
                    key="price_cents", range=models.Range(lte=filters.max_price_cents)
                )
            )

    exclude = list(exclude_ids)
    must_not = [models.HasIdCondition(has_id=exclude)] if exclude else None
    if not must and not must_not:
        return None
    return models.Filter(must=must or None, must_not=must_not)


def _passes(
    doc: ProductDoc,
    *,
    exclude_ids: set[str],
    categories: list[str] | None,
    level: str | None,
    max_price_cents: int | None,
) -> bool:
    if doc.product_id in exclude_ids:
        return False
    if categories and doc.category not in categories:
        return False
    if level and doc.level != level:
        return False
    if max_price_cents is not None and doc.price_cents > max_price_cents:
        return False
    return True


def _filtered_ids(
    ordered_ids: list[str],
    corpus_by_id: dict[str, ProductDoc],
    filters: RetrievalFilters,
    exclude_ids: set[str],
) -> list[str]:
    """Apply the metadata filter, relaxing (price → level → category) until the pool is healthy.

    The already-seen exclusion is never relaxed — recommending a course the user already engaged with
    is never desirable. Category/level/price are progressively dropped so a too-narrow plan proposal
    can't starve ``generate``/fallback of the 3+ grounded candidates they need.
    """
    variants = [
        (filters.categories, filters.level, filters.max_price_cents),
        (filters.categories, filters.level, None),
        (filters.categories, None, None),
        (None, None, None),
    ]
    survivors: list[str] = []
    for categories, level, max_price in variants:
        survivors = [
            pid
            for pid in ordered_ids
            if pid in corpus_by_id
            and _passes(
                corpus_by_id[pid],
                exclude_ids=exclude_ids,
                categories=categories,
                level=level,
                max_price_cents=max_price,
            )
        ]
        if len(survivors) >= _MIN_POOL:
            break
    return survivors


# --------------------------------------------------------------------------------------------------
# The pipeline
# --------------------------------------------------------------------------------------------------
def hybrid_retrieve(
    *,
    queries: list[str],
    query_vectors: list[list[float]],
    centroid: list[float] | None,
    corpus: list[ProductDoc],
    doc_vectors: dict[str, list[float]],
    filters: RetrievalFilters,
    exclude_ids: set[str],
    extra_ranked_lists: list[list[str]] | None = None,
    top_k: int = _TOP_K,
    mmr_lambda: float = _MMR_LAMBDA,
) -> list[Candidate]:
    """Dense + BM25 → RRF → metadata filter → MMR(λ) → top-k. Pure and deterministic.

    ``query_vectors`` are the per-query dense vectors (embedded by the node via Mesh); ``centroid`` is
    the user's interest vector used as an additional dense anchor; ``extra_ranked_lists`` lets the node
    inject Qdrant-native filtered-search rankings into the fusion. Returns MMR-ordered ``Candidate``s.
    """
    corpus_by_id = {doc.product_id: doc for doc in corpus}
    corpus_ids = [doc.product_id for doc in corpus]

    ranked_lists: list[list[str]] = list(extra_ranked_lists or [])

    # Dense leg: centroid anchor + one ranking per query vector (in-process cosine over Qdrant vecs).
    dense_queries: list[list[float]] = []
    if centroid:
        dense_queries.append(centroid)
    dense_queries.extend(v for v in query_vectors if v)
    for vec in dense_queries:
        ranking = _dense_ranking(vec, corpus_ids, doc_vectors)
        if ranking:
            ranked_lists.append(ranking)

    # Lexical leg: one BM25 ranking per query.
    for query in queries:
        ranking = _bm25_ranking(query, corpus)
        if ranking:
            ranked_lists.append(ranking)

    rrf_scores = _reciprocal_rank_fusion(ranked_lists)
    if not rrf_scores:
        # Nothing matched (e.g. empty queries + no vectors) — fall back to the whole active corpus so
        # downstream still has grounded options; ordering is arbitrary but the filter/MMR still apply.
        rrf_scores = {pid: 0.0 for pid in corpus_ids}

    ordered = sorted(rrf_scores.keys(), key=lambda pid: rrf_scores.get(pid, 0.0), reverse=True)
    survivors = _filtered_ids(ordered, corpus_by_id, filters, exclude_ids)
    selected = _mmr(survivors, rrf_scores, doc_vectors, lambda_=mmr_lambda, top_k=top_k)

    candidates: list[Candidate] = []
    for pid in selected:
        doc = corpus_by_id[pid]
        candidates.append(
            Candidate(
                product_id=doc.product_id,
                title=doc.title,
                category=doc.category,
                level=doc.level,
                price_cents=doc.price_cents,
                tags=list(doc.tags),
                score=rrf_scores.get(pid, 0.0),
            )
        )
    logger.info(
        "agent.retrieve",
        extra={
            "extra_fields": {
                "queries": len(queries),
                "ranked_lists": len(ranked_lists),
                "survivors": len(survivors),
                "candidates": len(candidates),
            }
        },
    )
    return candidates
