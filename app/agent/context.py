"""Run context, data loaders, telemetry, and shared helpers for the agent (Phase 6).

``run_agent`` (in ``graph.py``) does all of the run's I/O *up front* — load the active catalog from
Postgres, rebuild the decayed profile, gather the recent-event evidence — and packs the results into
an ``AgentContext``. That context is passed to the graph via ``config["configurable"]["ctx"]`` (config
is not checkpointed, so live objects like the Qdrant store ride along without serialization concerns),
and the nodes read from it. This keeps the graph nodes pure functions of ``(state, ctx)``: the only
external calls left inside the graph are the Mesh completions (plan/grade/refine/generate) and, in the
``retrieve`` node, the read-only Qdrant lookups — which makes the whole graph unit-testable offline
with a fake store and ``MESH_DISABLED=true`` (see ``tests/test_grounding.py``).

Nothing here constructs an AI client. The only model access anywhere in the agent is ``app.llm.mesh``
(invariants #1/#7). ``load_profile_bundle`` reuses ``app.services.profile.rebuild_profile`` (LLM-free;
it averages pre-computed Qdrant vectors) and pre-computed product vectors — never an embedding call.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select

from app.core.logging import get_logger
from app.db.models import Event, Product
from app.llm.mesh import MeshUsage
from app.services.profile import ProfileSnapshot, rebuild_profile
from app.vector.hybrid_retriever import ProductDoc, RetrievalFilters
from app.vector.qdrant_client import QdrantVectorStore, VectorStore

logger = get_logger(__name__)

# --- hard caps (non-negotiable — unbounded loops burn quota and hang the demo, PRD §6.4) ---
TOTAL_TIMEOUT_SECONDS = 25.0
GRADE_THRESHOLD = 0.7
MAX_REFINE_LOOPS = 2
MAX_GENERATE_ATTEMPTS = 2  # initial generation + exactly one retry

_EVIDENCE_LIMIT = 20        # recent events surfaced to prompts as "evidence"
_EVIDENCE_QUERY_LIMIT = 40  # rows scanned to build evidence (most-recent first)


# --------------------------------------------------------------------------------------------------
# Telemetry — aggregates every Mesh call's usage into the agent_runs row (PRD §6.8)
# --------------------------------------------------------------------------------------------------
class UsageCollector:
    """Collects ``MeshUsage`` from every node's Mesh call so the run can persist models/cost/tokens."""

    def __init__(self) -> None:
        self._items: list[MeshUsage] = []

    def record(self, usage: MeshUsage) -> None:
        """The ``on_usage`` recorder passed into every ``mesh.*`` call (sync; mesh awaits if needed)."""
        self._items.append(usage)

    def models_used(self) -> dict[str, Any]:
        """Per-model rollup for ``agent_runs.models_used`` — calls, tokens, cost, and which nodes."""
        by_model: dict[str, dict[str, Any]] = {}
        for usage in self._items:
            entry = by_model.setdefault(
                usage.model,
                {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0, "cost_usd": 0.0, "nodes": []},
            )
            entry["calls"] += 1
            entry["prompt_tokens"] += usage.prompt_tokens
            entry["completion_tokens"] += usage.completion_tokens
            entry["cost_usd"] = round(entry["cost_usd"] + usage.cost_usd, 6)
            if usage.node not in entry["nodes"]:
                entry["nodes"].append(usage.node)
        return by_model

    def total_cost(self) -> float:
        return round(sum(usage.cost_usd for usage in self._items), 6)


# --------------------------------------------------------------------------------------------------
# Run context
# --------------------------------------------------------------------------------------------------
@dataclass(slots=True)
class ProfileBundle:
    """What ``load_profile_bundle`` returns: the decayed snapshot + compact evidence + seen ids."""

    snapshot: ProfileSnapshot
    evidence: list[dict[str, Any]]
    seen_ids: set[str]


@dataclass(slots=True)
class AgentContext:
    """Per-run dependencies + pre-loaded data the nodes read (passed via config, never checkpointed)."""

    vector_store: VectorStore
    corpus: list[ProductDoc]
    interest_vector: list[float]
    profile_snapshot: ProfileSnapshot
    recent_events: list[dict[str, Any]]
    seen_product_ids: set[str]
    on_usage: Any = None
    deadline: float = field(default_factory=lambda: time.monotonic() + TOTAL_TIMEOUT_SECONDS)
    enable_rerank: bool = False
    remap_placeholders: bool = True
    _doc_vectors: dict[str, list[float]] | None = None
    _corpus_by_id: dict[str, ProductDoc] | None = None

    @property
    def corpus_by_id(self) -> dict[str, ProductDoc]:
        if self._corpus_by_id is None:
            self._corpus_by_id = {doc.product_id: doc for doc in self.corpus}
        return self._corpus_by_id

    @property
    def active_ids(self) -> set[str]:
        """The active-in-Postgres id set the grounding gate checks against (corpus == active rows)."""
        return set(self.corpus_by_id.keys())

    async def doc_vectors(self) -> dict[str, list[float]]:
        """Pre-computed product vectors from Qdrant (read-only), cached for the run's refine loops."""
        if self._doc_vectors is None:
            try:
                self._doc_vectors = await self.vector_store.retrieve_vectors(
                    [doc.product_id for doc in self.corpus]
                )
            except Exception as exc:  # pragma: no cover - degrade to lexical-only ranking on failure
                logger.warning("agent.doc_vectors_failed", extra={"extra_fields": {"error": str(exc)}})
                self._doc_vectors = {}
        return self._doc_vectors


def get_ctx(config: Any) -> AgentContext:
    """Extract the ``AgentContext`` a node needs from the LangGraph ``config`` it is invoked with."""
    return config["configurable"]["ctx"]


def over_deadline(ctx: AgentContext) -> bool:
    """True once the run has exceeded its wall-clock budget — nodes short-circuit to the fallback."""
    return time.monotonic() > ctx.deadline


# --------------------------------------------------------------------------------------------------
# Data loaders (run before the graph; all LLM-free)
# --------------------------------------------------------------------------------------------------
async def load_corpus(session: Any) -> list[ProductDoc]:
    """Load every active catalog row as ``ProductDoc`` — the BM25 corpus + grounding/active set."""
    rows = (await session.execute(select(Product).where(Product.is_active.is_(True)))).scalars().all()
    return [
        ProductDoc(
            product_id=str(product.id),
            title=product.title,
            description=product.description or "",
            category=product.category,
            tags=list(product.tags or []),
            level=product.level,
            price_cents=product.price_cents,
        )
        for product in rows
    ]


async def load_profile_bundle(
    session: Any, vector_store: VectorStore, user_id: uuid.UUID
) -> ProfileBundle:
    """Rebuild the decayed profile and gather compact recent-event evidence (both LLM-free)."""
    snapshot = await rebuild_profile(session, user_id, vector_store=vector_store)

    stmt = (
        select(Event, Product.title, Product.category)
        .outerjoin(Product, Product.id == Event.product_id)
        .where(Event.user_id == user_id)
        .order_by(Event.server_ts.desc())
        .limit(_EVIDENCE_QUERY_LIMIT)
    )
    rows = (await session.execute(stmt)).all()

    evidence: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for event, title, category in rows:
        if event.event_type == "search":
            query = str((event.payload or {}).get("query", "")).strip()
            if query:
                evidence.append({"type": "search", "query": query, "weight": float(event.weight)})
        elif event.product_id is not None:
            pid = str(event.product_id)
            seen_ids.add(pid)
            evidence.append(
                {
                    "type": event.event_type,
                    "product_id": pid,
                    "title": title or "",
                    "category": category or "",
                    "weight": float(event.weight),
                }
            )
    return ProfileBundle(snapshot=snapshot, evidence=evidence[:_EVIDENCE_LIMIT], seen_ids=seen_ids)


# --------------------------------------------------------------------------------------------------
# Small node helpers
# --------------------------------------------------------------------------------------------------
def catalog_facets(ctx: AgentContext) -> dict[str, list[str]]:
    """The distinct categories/levels in the active catalog — so plan/refine propose valid filters."""
    categories = sorted({doc.category for doc in ctx.corpus})
    levels = sorted({doc.level for doc in ctx.corpus})
    return {"categories": categories, "levels": levels}


def filters_to_dict(filters: Any) -> dict[str, Any]:
    """Serialize a ``PlanFilters`` into the plain dict carried in state."""
    return {
        "category": getattr(filters, "category", None),
        "level": getattr(filters, "level", None),
        "max_price_cents": getattr(filters, "max_price_cents", None),
    }


def dict_to_filters(data: dict[str, Any]) -> RetrievalFilters:
    """Widen the state's single-category filter dict into a ``RetrievalFilters`` (category set)."""
    category = (data or {}).get("category")
    categories = [category] if category else None
    return RetrievalFilters(
        categories=categories,
        level=(data or {}).get("level"),
        max_price_cents=(data or {}).get("max_price_cents"),
    )


def fallback_queries(profile: dict[str, Any]) -> list[str]:
    """Deterministic query fallback from the profile when the planner returns nothing usable."""
    terms = list((profile.get("top_terms") or {}).keys())[:3]
    categories = list((profile.get("top_categories") or {}).keys())[:2]
    queries = [f"{cat} courses" for cat in categories]
    if terms:
        queries.append(" ".join(terms))
    return queries or ["popular courses"]


def remap_placeholders(items: list[dict[str, Any]], candidate_ids: list[str]) -> list[dict[str, Any]]:
    """Map fixture sentinel ids (``PLACEHOLDER_PRODUCT_N``) onto real candidates for offline runs.

    This is an offline-convenience shim ONLY: it fires exclusively for the ``PLACEHOLDER_PRODUCT_<n>``
    sentinel the ``MESH_DISABLED`` fixture emits, mapping the n-th placeholder to the n-th retrieved
    candidate so the offline happy path yields grounded output. It NEVER rewrites an arbitrary model
    id — a genuinely hallucinated online id is left untouched and rejected by the grounding gate. The
    gate, not this shim, is the invariant.
    """
    import re

    pattern = re.compile(r"^PLACEHOLDER_PRODUCT_(\d+)$")
    remapped: list[dict[str, Any]] = []
    for item in items:
        match = pattern.match(str(item.get("product_id", "")))
        if match:
            index = int(match.group(1)) - 1
            if 0 <= index < len(candidate_ids):
                item = {**item, "product_id": candidate_ids[index]}
        remapped.append(item)
    return remapped


_MIN_PERSIST_ITEMS = 3  # the 3-5 item guarantee (mirrors the grounding gate / fallback)


def enrich_items(
    items: list[dict[str, Any]],
    corpus_by_id: dict[str, ProductDoc],
    active_ids: set[str] | None = None,
) -> list[dict[str, Any]]:
    """Attach catalog metadata to grounded items for storage/rendering (product_id stays the key).

    Persist-time defense-in-depth (invariant #2): when ``active_ids`` is supplied, items whose
    ``product_id`` is not active in the run's catalog snapshot are dropped *before* enrichment — a last
    line so a future upstream bug can never persist a bare/inactive id. Every live path already emits
    only grounded ids (the grounding gate + the grounded-by-construction fallback), so on those paths
    nothing is dropped and the output is byte-for-byte unchanged. If filtering ever drops below the
    3-item guarantee we log it rather than crash (the fallback should have prevented that).
    """
    if active_ids is not None:
        kept = [item for item in items if str(item.get("product_id")) in active_ids]
        if len(kept) != len(items):
            logger.warning(
                "agent.enrich_dropped_inactive_ids",
                extra={
                    "extra_fields": {
                        "dropped": len(items) - len(kept),
                        "kept": len(kept),
                        "below_min": len(kept) < _MIN_PERSIST_ITEMS,
                    }
                },
            )
        items = kept

    enriched: list[dict[str, Any]] = []
    for item in items:
        doc = corpus_by_id.get(str(item.get("product_id")))
        enriched.append(
            {
                "product_id": str(item.get("product_id")),
                "reason": item.get("reason", ""),
                "title": doc.title if doc else "",
                "category": doc.category if doc else "",
                "level": doc.level if doc else "",
                "price_cents": doc.price_cents if doc else 0,
            }
        )
    return enriched


def default_vector_store() -> VectorStore:
    """Construct the real Qdrant-backed store (run_agent's default when none is injected)."""
    return QdrantVectorStore()
