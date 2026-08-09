"""Pydantic structured-output schemas for the LangGraph agent (PRD §6.4).

Two families live here:

  * **Node LLM outputs** — the JSON contracts each Mesh-backed node validates its completion into
    (``PlanQueriesOutput``, ``GradeOutput``, ``GenerateOutput``, ``RerankOutput``). Their field names
    match the offline fixtures in ``app/llm/fixtures/*.json`` exactly so ``MESH_DISABLED=true`` runs
    the whole graph with zero network I/O (see the fixture files + ``app/llm/mesh.py``).
  * **The public result** — ``Recommendation`` / ``RecommendationItem``: the grounded, validated shape
    ``run_agent`` returns and Phases 7/8 consume. Every ``product_id`` in ``items`` is guaranteed by
    ``validate_grounding`` (the structural gate) to reference a real, active catalog row (invariant #2).

Nothing here constructs an AI client — that is only ever ``app/llm/mesh.py`` (invariants #1/#7).
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field, field_validator


# --------------------------------------------------------------------------------------------------
# Node LLM output schemas (must mirror app/llm/fixtures/*.json field-for-field)
# --------------------------------------------------------------------------------------------------
class PlanFilters(BaseModel):
    """Metadata filter proposal from ``plan_queries`` / ``refine_queries`` (fixture: ``filters``).

    ``category`` is a single optional category label (matches the fixture); the retriever widens it
    into a one-element category set. All fields are optional — an absent filter means "do not
    constrain on this axis", which is the safe default that keeps retrieval from over-narrowing.
    """

    category: str | None = None
    level: str | None = None
    max_price_cents: int | None = None


class PlanQueriesOutput(BaseModel):
    """``plan_queries`` / ``refine_queries`` output: 2-3 retrieval queries + a filter proposal."""

    queries: list[str] = Field(default_factory=list)
    filters: PlanFilters = Field(default_factory=PlanFilters)
    rationale: str = ""


class GradeDecision(BaseModel):
    """Per-candidate keep/drop decision from ``grade_retrieval`` (fixture: ``decisions[]``)."""

    product_id: str
    keep: bool = True


class GradeOutput(BaseModel):
    """``grade_retrieval`` output: a 0-1 self-grade, a gap description, and per-candidate decisions."""

    score: float = 0.0
    gap: str = ""
    decisions: list[GradeDecision] = Field(default_factory=list)

    @field_validator("score")
    @classmethod
    def _clamp_score(cls, value: float) -> float:
        return max(0.0, min(1.0, float(value)))


class GenerateItem(BaseModel):
    """One recommended item from ``generate``: a ``product_id`` + a per-item ``reason`` (fixture)."""

    product_id: str
    reason: str


class GenerateOutput(BaseModel):
    """``generate`` output: ``{headline, narrative, items:[{product_id, reason}]}`` (fixture shape)."""

    headline: str
    narrative: str
    items: list[GenerateItem] = Field(default_factory=list)


class RerankEntry(BaseModel):
    """One pairwise-rerank score from ``rerank`` (fixture: ``ranking[]``)."""

    product_id: str
    score: float = 0.0


class RerankOutput(BaseModel):
    """``rerank`` output: a re-scored candidate ranking (fixture shape)."""

    ranking: list[RerankEntry] = Field(default_factory=list)


# --------------------------------------------------------------------------------------------------
# Public result — the grounded recommendation returned by run_agent (Phases 7/8 import this)
# --------------------------------------------------------------------------------------------------
class RecommendationItem(BaseModel):
    """A single grounded recommendation card. ``product_id`` always references an active catalog row.

    Enriched with catalog metadata (``title``/``category``/``level``/``price_cents``) so the dashboard
    (Phase 8) and the digest (Phase 9) can render without a second join — but ``product_id`` remains
    the authoritative key the grounding gate validated.
    """

    product_id: str
    reason: str
    title: str = ""
    category: str = ""
    level: str = ""
    price_cents: int = 0


class Recommendation(BaseModel):
    """The full result of one agent run: a grounded recommendation plus its run telemetry.

    This is the return type of ``app.agent.graph.run_agent`` and the contract Phase 7 (trigger/cache)
    and Phase 8 (dashboard) build on. ``grounded`` is always ``True`` by construction — the graph
    never returns an ungrounded item (invariant #2); ``fallback_used`` records whether the
    deterministic top-K path produced it. ``node_path`` is the ordered list of nodes actually
    executed (including refine loops and the fallback), the same value written to ``agent_runs``.
    """

    user_id: str
    headline: str
    narrative: str
    items: list[RecommendationItem]
    trigger_reason: str
    grounded: bool = True
    fallback_used: bool = False
    node_path: list[str] = Field(default_factory=list)
    retrieval_score: float | None = None
    refine_loops: int = 0
    cache_hit: str | None = None
    models_used: dict = Field(default_factory=dict)
    cost_usd: float = 0.0
    latency_ms: int = 0
    run_id: str = ""
    status: str = "success"
    recommendation_id: str | None = None
    agent_run_id: str | None = None
    created_at: datetime | None = None
