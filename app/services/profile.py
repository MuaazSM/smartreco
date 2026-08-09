"""Incremental behavioral profile builder (PRD §6.3; IMPLEMENTATION.md Phase 5). LLM-free.

Runs *after* event ingest, off the hot request path, and never calls Mesh — the interest centroid is
built from the **pre-computed** product vectors already living in Qdrant (Phase 4 populated them via
the outbox), not by embedding anything here. Embedding would be an AI call and violate invariant #6/#7.

What one recompute produces and persists to ``user_profiles`` (one row per user, upserted):

  * ``interest_vector`` — the **decayed interest centroid**: a weighted average of the Qdrant vectors
    of every product the user engaged with, each weighted by ``event_weight * 0.5**(age_days / 3)``
    (a **3-day half-life**), so last night's clicks dominate last week's. Empty until the user has
    engaged with at least one product that has a synced vector.
  * ``top_categories`` / ``top_terms`` — decayed-weighted frequency maps (category → weight from
    product events; search term → weight from ``search`` events), sorted strongest-first.
  * ``profile_hash`` — a stable hash of the profile inputs; the L1 exact-cache key the Phase 7 trigger
    policy compares against. It moves exactly when the centroid / categories / terms / level affinity
    move, and not otherwise.
  * ``events_since_gen`` — incremented atomically by the number of newly-ingested events; the
    "8+ new events" arm of the trigger. Only a generation run (Phase 7) resets it to 0.

Deliberately **not** a persisted column (the ``user_profiles`` schema has none, and Phase 5 does not
alter the schema): **level affinity**. It is still computed and returned on the in-memory
``ProfileSnapshot`` (and folded into ``profile_hash``) for callers that want it; the agent's
``build_profile`` node (Phase 6) also derives it live from events. See ``ProfileSnapshot``.

Drift accessor (Phase 7): ``cosine_distance(a, b)`` is the pure function the trigger's ``drift > 0.15``
arm calls. ``rebuild_profile`` also returns ``drift`` = the distance the centroid moved since the
*previously persisted* vector, for observability; the trigger measures drift since the last
*generation* by holding that reference itself (e.g. in Redis) and calling ``cosine_distance``.
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone

import numpy as np
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql import func

from app.core.logging import get_logger
from app.db.models import Event, Product, UserProfile
from app.vector.qdrant_client import QdrantVectorStore, VectorStore

logger = get_logger(__name__)

HALF_LIFE_DAYS = 3.0
# Only events this recent feed the profile. 30 days ≈ 10 half-lives (a 2**-10 ≈ 0.001 weight floor),
# so anything older is numerically negligible anyway; the bound keeps the recompute query cheap and
# lets it ride the (user_id, server_ts DESC) index.
_RECENT_WINDOW_DAYS = 30
# Event types that reference a product and therefore feed the centroid / categories / level affinity.
_PRODUCT_EVENT_TYPES = frozenset({"view", "click", "dwell", "cart"})
# Keep the top_terms map bounded so profile_hash stays stable-sized and JSONB stays small.
_MAX_TERMS = 25
# Rounding used for the profile_hash so floating-point jitter never spuriously changes the L1 key.
_WEIGHT_ROUND = 4
_VECTOR_ROUND = 4

# Minimal stopword set so search terms like "the"/"a"/"for" don't dominate top_terms.
_STOPWORDS = frozenset(
    {
        "the", "a", "an", "and", "or", "for", "to", "of", "in", "on", "with", "how",
        "what", "is", "are", "my", "me", "i", "vs", "using", "use", "best", "learn",
    }
)
_TOKEN_RE = re.compile(r"[a-z0-9]+")


@dataclass(slots=True)
class ProfileSnapshot:
    """The freshly-recomputed profile for one user — the shape Phase 7 and the agent consume.

    ``interest_vector`` is the persisted decayed centroid (``[]`` when the user has no product
    engagement with a synced vector). ``level_affinity`` is computed and returned here but is *not*
    a persisted column (see module docstring). ``drift`` is the cosine distance the centroid moved
    since the previously-persisted vector (``0.0`` when there was none or either side is empty).
    """

    user_id: uuid.UUID
    interest_vector: list[float]
    top_categories: dict[str, float]
    top_terms: dict[str, float]
    level_affinity: dict[str, float]
    events_since_gen: int
    profile_hash: str
    previous_interest_vector: list[float] = field(default_factory=list)
    drift: float = 0.0
    considered_events: int = 0


def cosine_distance(a: list[float] | None, b: list[float] | None) -> float:
    """Cosine distance ``1 - cos(a, b)`` in ``[0, 2]`` — the trigger policy's drift measure.

    Returns ``0.0`` when either vector is missing/empty/zero-norm (no measurable drift), so an
    unpopulated profile never spuriously reads as "drifted". A distance near 0 means "same interests";
    > 0.15 is the PRD's regeneration threshold.
    """
    if not a or not b:
        return 0.0
    va = np.asarray(a, dtype=np.float64)
    vb = np.asarray(b, dtype=np.float64)
    if va.shape != vb.shape:
        return 0.0
    na = float(np.linalg.norm(va))
    nb = float(np.linalg.norm(vb))
    if na == 0.0 or nb == 0.0:
        return 0.0
    cos = float(np.dot(va, vb) / (na * nb))
    cos = max(-1.0, min(1.0, cos))
    return 1.0 - cos


def _decay_factor(client_ts: datetime, now: datetime) -> float:
    """``0.5 ** (age_days / half_life)`` for an event, clamped so future timestamps never exceed 1.0."""
    age_seconds = (now - client_ts).total_seconds()
    if age_seconds <= 0:
        return 1.0
    age_days = age_seconds / 86_400.0
    return float(0.5 ** (age_days / HALF_LIFE_DAYS))


def _tokenize(query: str) -> list[str]:
    """Lowercase, split on non-alphanumerics, drop stopwords and 1-char noise — search → terms."""
    return [
        token
        for token in _TOKEN_RE.findall(query.lower())
        if len(token) > 1 and token not in _STOPWORDS
    ]


def _sorted_map(weights: dict[str, float], *, limit: int | None = None) -> dict[str, float]:
    """Round, then sort a weight map strongest-first (ties broken by label) for stable output."""
    rounded = {k: round(v, _WEIGHT_ROUND) for k, v in weights.items() if v > 0}
    ordered = sorted(rounded.items(), key=lambda kv: (-kv[1], kv[0]))
    if limit is not None:
        ordered = ordered[:limit]
    return dict(ordered)


def _profile_hash(
    interest_vector: list[float],
    top_categories: dict[str, float],
    top_terms: dict[str, float],
    level_affinity: dict[str, float],
) -> str:
    """A deterministic hash of the profile inputs — the Phase 7 L1 exact-cache key.

    The interest vector is quantized before hashing so floating-point summation jitter can never
    change the key while the profile is semantically unchanged (PRD §6.5 L1 cache contract).
    """
    signature = {
        "v": [round(x, _VECTOR_ROUND) for x in interest_vector],
        "c": top_categories,
        "t": top_terms,
        "l": level_affinity,
    }
    blob = json.dumps(signature, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


async def _load_recent_events(
    session: AsyncSession, user_id: uuid.UUID
) -> list[tuple[Event, str | None, str | None]]:
    """Recent events for the user, each paired with its product's category/level (or None)."""
    cutoff = func.now() - func.make_interval(0, 0, 0, _RECENT_WINDOW_DAYS)
    stmt = (
        select(Event, Product.category, Product.level)
        .outerjoin(Product, Product.id == Event.product_id)
        .where(Event.user_id == user_id, Event.server_ts >= cutoff)
        .order_by(Event.id)
    )
    rows = await session.execute(stmt)
    return [(row[0], row[1], row[2]) for row in rows.all()]


async def _fetch_vectors(
    vector_store: VectorStore, product_ids: set[uuid.UUID]
) -> dict[str, list[float]]:
    """Pre-computed Qdrant vectors for the engaged products (read-only; empty set → no call)."""
    if not product_ids:
        return {}
    return await vector_store.retrieve_vectors(product_ids)


def _compute(
    events: list[tuple[Event, str | None, str | None]],
    vectors: dict[str, list[float]],
    now: datetime,
) -> tuple[list[float], dict[str, float], dict[str, float], dict[str, float]]:
    """Pure core: fold decayed events into (centroid, top_categories, top_terms, level_affinity)."""
    category_weights: dict[str, float] = {}
    term_weights: dict[str, float] = {}
    level_weights: dict[str, float] = {}

    centroid_num: np.ndarray | None = None
    centroid_den = 0.0

    for event, category, level in events:
        decay = _decay_factor(event.client_ts, now)
        weight = float(event.weight) * decay

        if event.event_type == "search":
            for term in _tokenize(str((event.payload or {}).get("query", ""))):
                term_weights[term] = term_weights.get(term, 0.0) + weight
            continue

        if event.event_type in _PRODUCT_EVENT_TYPES and event.product_id is not None:
            if category:
                category_weights[category] = category_weights.get(category, 0.0) + weight
            if level:
                level_weights[level] = level_weights.get(level, 0.0) + weight

            vector = vectors.get(str(event.product_id))
            if vector:
                vec = np.asarray(vector, dtype=np.float64)
                if centroid_num is None:
                    centroid_num = np.zeros_like(vec)
                if vec.shape == centroid_num.shape:
                    centroid_num += weight * vec
                    centroid_den += weight

    interest_vector: list[float] = []
    if centroid_num is not None and centroid_den > 0:
        interest_vector = (centroid_num / centroid_den).astype(np.float32).tolist()

    return (
        interest_vector,
        _sorted_map(category_weights),
        _sorted_map(term_weights, limit=_MAX_TERMS),
        _sorted_map(level_weights),
    )


async def rebuild_profile(
    session: AsyncSession,
    user_id: uuid.UUID,
    *,
    new_event_count: int = 0,
    vector_store: VectorStore | None = None,
    now: datetime | None = None,
) -> ProfileSnapshot:
    """Recompute the decayed profile from recent events + Qdrant vectors and upsert ``user_profiles``.

    ``new_event_count`` (the number of rows the preceding ingest actually inserted) is added
    atomically to ``events_since_gen``. ``last_generated_at`` is untouched — only a generation run
    (Phase 7) sets that and resets the counter. Makes no LLM/embedding call. Returns a
    ``ProfileSnapshot`` carrying the drift versus the previously-persisted centroid.
    """
    now = now or datetime.now(timezone.utc)
    owns_store = vector_store is None
    store: VectorStore = vector_store or QdrantVectorStore()
    try:
        events = await _load_recent_events(session, user_id)
        product_ids = {
            e.product_id
            for e, _c, _l in events
            if e.product_id is not None and e.event_type in _PRODUCT_EVENT_TYPES
        }
        vectors = await _fetch_vectors(store, product_ids)
    finally:
        if owns_store:
            await store.close()

    interest_vector, top_categories, top_terms, level_affinity = _compute(events, vectors, now)
    profile_hash = _profile_hash(interest_vector, top_categories, top_terms, level_affinity)

    existing = await session.get(UserProfile, user_id)
    previous_vector = list(existing.interest_vector) if existing else []

    delta = max(0, new_event_count)
    stmt = pg_insert(UserProfile).values(
        user_id=user_id,
        interest_vector=interest_vector,
        top_categories=top_categories,
        top_terms=top_terms,
        profile_hash=profile_hash,
        events_since_gen=delta,
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=[UserProfile.user_id],
        set_={
            "interest_vector": stmt.excluded.interest_vector,
            "top_categories": stmt.excluded.top_categories,
            "top_terms": stmt.excluded.top_terms,
            "profile_hash": stmt.excluded.profile_hash,
            # Atomic increment against the row's *existing* value — race-safe under concurrent batches.
            "events_since_gen": UserProfile.events_since_gen + delta,
            "updated_at": func.now(),
        },
    )
    await session.execute(stmt)
    await session.commit()

    events_since_gen = (existing.events_since_gen if existing else 0) + delta
    drift = cosine_distance(interest_vector, previous_vector)

    logger.info(
        "profile.rebuilt",
        extra={
            "extra_fields": {
                "user_id": str(user_id),
                "considered_events": len(events),
                "new_events": delta,
                "events_since_gen": events_since_gen,
                "dim": len(interest_vector),
                "top_categories": list(top_categories)[:3],
                "drift": round(drift, 4),
                "profile_hash": profile_hash[:12],
            }
        },
    )

    return ProfileSnapshot(
        user_id=user_id,
        interest_vector=interest_vector,
        top_categories=top_categories,
        top_terms=top_terms,
        level_affinity=level_affinity,
        events_since_gen=events_since_gen,
        profile_hash=profile_hash,
        previous_interest_vector=previous_vector,
        drift=drift,
        considered_events=len(events),
    )
