"""Per-node model routing for Mesh — the ONLY place a model id is chosen (PRD §6.5, §6.5.1).

Mesh exposes 1000+ models behind one gateway and flags each with `is_free`, so the routing table is
expressed as a *tier preference*, never a hardcoded id. A hardcoded id that gets delisted the night
before judging is an avoidable failure (PRD §6.5.1 rule 1): we resolve the free catalog from Mesh at
startup, cache it, and fall back down a preference chain to a paid model.

`resolve(node, tier)` is imported by `app.llm.mesh` and is the single decision point — call sites in
the agent never name a model. Under `MESH_DISABLED=true` this module performs **no** network call:
`free_model_ids()` returns a deterministic fixture catalog so the whole app runs offline.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Any

import httpx

from app.core.config import settings
from app.core.logging import get_logger

logger = get_logger(__name__)

# Ordered preference — first id that Mesh currently lists as free wins (PRD §6.5.1 rule 1). Ordered
# largest/strongest first so the `generate` node picks the best available free model for persuasion
# while the cheap nodes are equally happy with whatever is free.
FREE_PREFERENCE: list[str] = [
    "meta-llama/llama-3.3-70b-instruct",
    "qwen/qwen-2.5-72b-instruct",
    "deepseek/deepseek-chat",
    "mistralai/mistral-small",
    "google/gemma-2-9b-it",
]

# Paid fallback per tier when nothing free is available (PRD §6.5 routing table). Exact values are
# fixed by the phase spec — do not change without a corresponding README note.
PAID_FALLBACK: dict[str, str] = {
    "cheap": "openai/gpt-4o-mini",
    "quality": "anthropic/claude-sonnet-4.5",
}

# Embeddings are a fixed model, not a chat tier (PRD §6.5 routing table): 1536-dim, cents per catalog.
EMBEDDING_MODEL: str = "openai/text-embedding-3-small"

# Default tier per node so callers may pass just the node name (PRD §6.5 routing table):
#   plan_queries / grade_retrieval / rerank → cheap free instruct
#   generate                                → quality (free large → paid quality)
#   embeddings                              → fixed embedding model
NODE_TIERS: dict[str, str] = {
    "plan_queries": "cheap",
    "grade_retrieval": "cheap",
    "rerank": "cheap",
    "generate": "quality",
    "embeddings": "cheap",
}

# Snapshot of Mesh's live free catalog, primed once by refresh_free_models() at startup. Empty until
# primed (with Mesh enabled) so resolve() falls back to paid rather than blocking on a network call.
_live_free_models: frozenset[str] = frozenset()


async def _fetch_model_catalog() -> list[dict[str, Any]]:
    """Fetch Mesh's raw ``/models`` catalog as a list of dicts, tolerating both response shapes.

    Mesh returns a **bare JSON list** (``[{"id": ..., "is_free": ...}, ...]``), not the OpenAI SDK's
    expected paginated envelope (``{"object": "list", "data": [...]}``). The SDK's typed
    ``client.models.list()`` assumes the latter and throws deep in its pagination code when handed the
    former (``AttributeError: 'list' object has no attribute '_set_private_attributes'``). We go
    around that by reusing the single Mesh client (CLAUDE.md #1) but asking for the raw
    ``httpx.Response`` instead of a parsed SDK model, then parsing the JSON ourselves and accepting
    either shape.
    """
    from app.llm.mesh import get_client  # lazy import: avoids a module-load cycle with mesh.py

    client = get_client()
    response = await client.get("/models", cast_to=httpx.Response)
    payload = response.json()
    if isinstance(payload, dict):
        payload = payload.get("data", [])
    if not isinstance(payload, list):
        raise TypeError(f"unexpected Mesh /models payload shape: {type(payload).__name__}")
    return [item for item in payload if isinstance(item, dict)]


async def refresh_free_models() -> frozenset[str]:
    """Resolve Mesh's free-tier catalog once and cache it (call from the app lifespan at startup).

    Under ``MESH_DISABLED=true`` this makes **no** network call and returns the deterministic fixture
    catalog (``FREE_PREFERENCE``). Otherwise it fetches Mesh's raw model catalog (see
    ``_fetch_model_catalog`` — tolerates a bare list or a ``{"data": [...]}`` envelope) and keeps the
    ids whose ``is_free`` flag is truthy. Any failure along the way — network error, non-2xx status
    (e.g. 402 on a balance-less account), timeout, or an unexpected payload shape — is logged and
    treated as "no free models known" rather than raised: startup must never crash because Mesh's
    catalog endpoint is unavailable, and ``resolve()`` cleanly falls back to ``PAID_FALLBACK`` instead.
    """
    global _live_free_models
    if settings.mesh_disabled:
        _live_free_models = frozenset(FREE_PREFERENCE)
        free_model_ids.cache_clear()
        logger.info("mesh_free_models_fixture", extra={"count": len(_live_free_models)})
        return _live_free_models

    try:
        models = await _fetch_model_catalog()
        ids = {
            model["id"]
            for model in models
            if model.get("is_free", False) and isinstance(model.get("id"), str)
        }
    except Exception:
        logger.exception("mesh_free_models_fetch_failed")
        _live_free_models = frozenset()
        free_model_ids.cache_clear()
        return _live_free_models

    _live_free_models = frozenset(ids)
    free_model_ids.cache_clear()
    logger.info("mesh_free_models_resolved", extra={"count": len(ids)})
    return _live_free_models


@lru_cache(maxsize=1)
def free_model_ids() -> frozenset[str]:
    """Cached snapshot of Mesh's free-tier model ids (resolved at startup, PRD §6.5.1 rule 1).

    Never performs I/O itself. Under ``MESH_DISABLED=true`` it returns the fixture catalog; with Mesh
    enabled it returns whatever ``refresh_free_models()`` primed. Before priming (Mesh enabled) it is
    empty and ``resolve()`` falls back to the paid tier — the sync signature cannot await the async
    client, so priming is done once at startup instead of lazily here.
    """
    if settings.mesh_disabled:
        return frozenset(FREE_PREFERENCE)
    return _live_free_models


def _tier_override(tier: str) -> str | None:
    """An explicit per-tier model id from settings, if configured (else None → free-first walk)."""
    if tier == "cheap":
        return settings.mesh_cheap_model
    if tier == "quality":
        return settings.mesh_quality_model
    return None


def resolve(node: str, tier: str | None = None) -> str:
    """Resolve the Mesh model id for ``node``. The single model-selection point (CLAUDE.md #1).

    Order: an explicit ``settings.mesh_{cheap,quality}_model`` override (used to pin fast paid models
    once a balance exists) wins; otherwise walk ``FREE_PREFERENCE`` and return the first id Mesh
    currently lists as free; if none are free (or the catalog is not yet primed) fall back to
    ``PAID_FALLBACK[tier]``. Embeddings always use ``EMBEDDING_MODEL``. ``tier`` defaults to the node's
    tier in ``NODE_TIERS`` (``cheap`` if unknown).
    """
    if node == "embeddings":
        return EMBEDDING_MODEL

    resolved_tier = tier or NODE_TIERS.get(node, "cheap")
    if resolved_tier not in PAID_FALLBACK:
        raise ValueError(f"unknown tier {resolved_tier!r} for node {node!r}")

    override = _tier_override(resolved_tier)
    if override:
        return override

    free = free_model_ids()
    for candidate in FREE_PREFERENCE:
        if candidate in free:
            return candidate
    return PAID_FALLBACK[resolved_tier]
