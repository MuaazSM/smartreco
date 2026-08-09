"""The LangGraph recommendation agent — wiring, conditional edges, and the ``run_agent`` entry point.

This is the centerpiece (PRD §6.4, Figure 5): a real seven-node graph with conditional edges, a
self-grading loop, a bounded refine loop, and a structural grounding gate that guarantees the system
never surfaces an ungrounded recommendation (invariant #2).

Topology::

    START → build_profile → plan_queries → retrieve → grade_retrieval
                                              ▲              │
                              refine_queries ─┘   score<0.7 & loops<2
                                              │
              score≥0.7 or loops exhausted →  generate → validate_grounding
                                                              │  valid → END
                                     invalid & attempts<2 →  generate (retry, errors injected)
                                     invalid & attempts=2 →  fallback → END

Hard caps (non-negotiable): refine loops ≤ 2, one generation retry (≤ 2 generate calls), and a 25s
wall-clock timeout enforced by ``asyncio.wait_for`` plus per-node deadline guards. Every run records
its full ``node_path`` (including loops and the fallback) to ``agent_runs`` — the proof the graph is
real — alongside retrieval score, refine loops, models/cost/tokens, latency, and status.

Checkpointer: a per-run ``MemorySaver`` (resumable/inspectable within the run). Swapping in the Postgres
checkpointer is a one-argument change (``run_agent(..., checkpointer=...)``); the ``node_path`` written
to ``agent_runs`` is the durable, inspectable record either way. See FOLLOWUPS in the phase report.

The ONLY model access anywhere below the entry point is ``app.llm.mesh`` (invariants #1/#7).
"""

from __future__ import annotations

import asyncio
import time
import uuid
from datetime import datetime, timezone
from typing import Any

from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph
from sqlalchemy import update

from app.agent import nodes
from app.agent.context import (
    GRADE_THRESHOLD,
    MAX_GENERATE_ATTEMPTS,
    MAX_REFINE_LOOPS,
    TOTAL_TIMEOUT_SECONDS,
    AgentContext,
    UsageCollector,
    default_vector_store,
    enrich_items,
    load_corpus,
    load_profile_bundle,
)
from app.agent.nodes.fallback import build_fallback_draft
from app.agent.schemas import Recommendation, RecommendationItem
from app.agent.state import AgentState
from app.core.logging import get_logger, set_run_id
from app.db.models import AgentRun
from app.db.models import Recommendation as RecommendationRow
from app.db.session import AsyncSessionLocal
from app.vector.qdrant_client import VectorStore

logger = get_logger(__name__)


# --------------------------------------------------------------------------------------------------
# Conditional routing (state-only; the 25s wait_for is the hard timeout, node guards are best-effort)
# --------------------------------------------------------------------------------------------------
def route_after_grade(state: dict[str, Any]) -> str:
    """grade ≥ 0.7 → generate; below with refine budget → refine; budget exhausted → generate."""
    score = state.get("retrieval_score", 0.0) or 0.0
    if score >= GRADE_THRESHOLD:
        return "generate"
    if state.get("refine_loops", 0) < MAX_REFINE_LOOPS:
        return "refine_queries"
    return "generate"


def route_after_validate(state: dict[str, Any]) -> str:
    """valid → END; invalid with a retry left → generate (errors injected); else → fallback."""
    if state.get("valid"):
        return "end"
    if state.get("generate_attempts", 0) < MAX_GENERATE_ATTEMPTS:
        return "generate"
    return "fallback"


# --------------------------------------------------------------------------------------------------
# Graph construction
# --------------------------------------------------------------------------------------------------
def _build_workflow() -> StateGraph:
    graph = StateGraph(AgentState)
    graph.add_node("build_profile", nodes.build_profile)
    graph.add_node("plan_queries", nodes.plan_queries)
    graph.add_node("retrieve", nodes.retrieve)
    graph.add_node("grade_retrieval", nodes.grade_retrieval)
    graph.add_node("refine_queries", nodes.refine_queries)
    graph.add_node("generate", nodes.generate)
    graph.add_node("validate_grounding", nodes.validate_grounding)
    graph.add_node("fallback", nodes.fallback)

    graph.add_edge(START, "build_profile")
    graph.add_edge("build_profile", "plan_queries")
    graph.add_edge("plan_queries", "retrieve")
    graph.add_edge("retrieve", "grade_retrieval")
    graph.add_conditional_edges(
        "grade_retrieval",
        route_after_grade,
        {"generate": "generate", "refine_queries": "refine_queries"},
    )
    graph.add_edge("refine_queries", "retrieve")
    graph.add_edge("generate", "validate_grounding")
    graph.add_conditional_edges(
        "validate_grounding",
        route_after_validate,
        {"end": END, "generate": "generate", "fallback": "fallback"},
    )
    graph.add_edge("fallback", END)
    return graph


def build_agent_graph(checkpointer: Any = None):
    """Compile the workflow with a checkpointer (defaults to a fresh in-memory ``MemorySaver``)."""
    return _build_workflow().compile(checkpointer=checkpointer or MemorySaver())


def initial_state(user_id: Any) -> AgentState:
    """A clean starting state (empty ``node_path`` so the reducer accumulates the real path)."""
    return {
        "user_id": str(user_id),
        "node_path": [],
        "refine_loops": 0,
        "generate_attempts": 0,
        "validation_errors": [],
        "candidates": [],
        "draft": None,
        "fallback_used": False,
        "valid": False,
    }


# --------------------------------------------------------------------------------------------------
# Result resolution + persistence
# --------------------------------------------------------------------------------------------------
def _emergency_profile(ctx: AgentContext) -> dict[str, Any]:
    snapshot = ctx.profile_snapshot
    return {
        "top_categories": dict(list(snapshot.top_categories.items())[:6]),
        "top_terms": dict(list(snapshot.top_terms.items())[:10]),
        "level_affinity": dict(snapshot.level_affinity),
        "seen_count": len(ctx.seen_product_ids),
    }


def _resolve_result(final: dict[str, Any] | None, ctx: AgentContext):
    """Return ``(draft, node_path, score, refine_loops, fallback_used)`` for a finished/timed-out run.

    A completed run yields a grounded draft from state. A timeout (``final is None``) or an empty draft
    triggers a corpus-derived deterministic fallback so the caller ALWAYS gets a grounded result — the
    grounding invariant holds even when the graph is cancelled mid-run.
    """
    if final and final.get("draft") and final["draft"].get("items"):
        return (
            final["draft"],
            list(final.get("node_path", [])),
            final.get("retrieval_score"),
            final.get("refine_loops", 0),
            bool(final.get("fallback_used")),
        )

    profile = _emergency_profile(ctx)
    candidates = (final.get("candidates") if final else None) or []
    draft = build_fallback_draft(ctx, profile, candidates)
    node_path = (list(final.get("node_path", [])) if final else []) + ["timeout_fallback"]
    score = final.get("retrieval_score") if final else None
    refine_loops = final.get("refine_loops", 0) if final else 0
    return draft, node_path, score, refine_loops, True


async def _persist(
    session_factory: Any,
    uid: uuid.UUID,
    run_id: str,
    trigger_reason: str,
    draft: dict[str, Any],
    node_path: list[str],
    score: float | None,
    refine_loops: int,
    fallback_used: bool,
    collector: UsageCollector,
    mono_start: float,
    ctx: AgentContext,
) -> Recommendation:
    """Write the ``agent_runs`` telemetry row and the new current ``recommendations`` row (one tx)."""
    latency_ms = int((time.monotonic() - mono_start) * 1000)
    status = "fallback" if fallback_used else "success"
    items_enriched = enrich_items(draft.get("items", []), ctx.corpus_by_id)
    models_used = collector.models_used()
    cost_usd = collector.total_cost()

    async with session_factory() as session:
        agent_run = AgentRun(
            run_id=run_id,
            user_id=uid,
            trigger_reason=trigger_reason,
            node_path=list(node_path),
            retrieval_score=score,
            refine_loops=refine_loops,
            cache_hit=None,  # Phase 7 sets 'l1'/'l2'; a full graph run is NULL
            models_used=models_used,
            cost_usd=cost_usd,
            latency_ms=latency_ms,
            status=status,
            completed_at=datetime.now(timezone.utc),
        )
        session.add(agent_run)
        await session.flush()

        # Exactly one current recommendation per user (WHERE is_current partial unique index).
        await session.execute(
            update(RecommendationRow)
            .where(RecommendationRow.user_id == uid, RecommendationRow.is_current.is_(True))
            .values(is_current=False)
        )
        rec_row = RecommendationRow(
            user_id=uid,
            agent_run_id=agent_run.id,
            headline=draft.get("headline", ""),
            narrative=draft.get("narrative", ""),
            items=items_enriched,
            trigger_reason=trigger_reason,
            is_current=True,
        )
        session.add(rec_row)
        await session.commit()
        await session.refresh(rec_row)
        recommendation_id = str(rec_row.id)
        agent_run_id = str(agent_run.id)
        created_at = rec_row.created_at

    return Recommendation(
        user_id=str(uid),
        headline=draft.get("headline", ""),
        narrative=draft.get("narrative", ""),
        items=[RecommendationItem(**item) for item in items_enriched],
        trigger_reason=trigger_reason,
        grounded=True,
        fallback_used=fallback_used,
        node_path=list(node_path),
        retrieval_score=score,
        refine_loops=refine_loops,
        cache_hit=None,
        models_used=models_used,
        cost_usd=cost_usd,
        latency_ms=latency_ms,
        run_id=run_id,
        status=status,
        recommendation_id=recommendation_id,
        agent_run_id=agent_run_id,
        created_at=created_at,
    )


# --------------------------------------------------------------------------------------------------
# Public entry point (Phases 7/8 call this)
# --------------------------------------------------------------------------------------------------
async def run_agent(
    user_id: str | uuid.UUID,
    *,
    session_factory: Any = AsyncSessionLocal,
    vector_store: VectorStore | None = None,
    trigger_reason: str = "manual",
    checkpointer: Any = None,
    enable_rerank: bool = False,
    remap_placeholders: bool = True,
) -> Recommendation:
    """Run the graph for ``user_id``, persist the result, and return the grounded ``Recommendation``.

    Prepares the run (loads the active catalog, rebuilds the decayed profile, gathers evidence),
    executes the seven-node graph under a 25s cap, then persists the recommendation (as the user's
    single ``is_current`` row) and the ``agent_runs`` telemetry. The returned ``Recommendation`` is
    grounded by construction — every ``product_id`` references an active catalog row (invariant #2).

    Args:
        user_id: the learner to recommend for.
        session_factory: async session factory (defaults to the app's ``AsyncSessionLocal``).
        vector_store: injectable Qdrant store (a throwaway one is built + closed when omitted).
        trigger_reason: recorded on ``agent_runs`` ('manual' | 'scheduled' | 'event' | ...).
        checkpointer: LangGraph checkpointer (defaults to a fresh ``MemorySaver``).
        enable_rerank: turn on the optional cheap pairwise reranker (default off, near-zero cost).
        remap_placeholders: offline convenience — map fixture sentinel ids onto real candidates.

    Returns:
        A ``Recommendation`` with the grounded items plus full run telemetry (``node_path``, score,
        refine loops, models/cost, latency, status).
    """
    run_id = set_run_id()
    uid = user_id if isinstance(user_id, uuid.UUID) else uuid.UUID(str(user_id))
    mono_start = time.monotonic()
    own_store = vector_store is None
    store: VectorStore = vector_store or default_vector_store()
    collector = UsageCollector()

    logger.info(
        "agent.run_start",
        extra={"extra_fields": {"user_id": str(uid), "trigger_reason": trigger_reason}},
    )
    try:
        async with session_factory() as session:
            corpus = await load_corpus(session)
            bundle = await load_profile_bundle(session, store, uid)

        ctx = AgentContext(
            vector_store=store,
            corpus=corpus,
            interest_vector=list(bundle.snapshot.interest_vector or []),
            profile_snapshot=bundle.snapshot,
            recent_events=bundle.evidence,
            seen_product_ids=set(bundle.seen_ids),
            on_usage=collector.record,
            deadline=mono_start + TOTAL_TIMEOUT_SECONDS,
            enable_rerank=enable_rerank,
            remap_placeholders=remap_placeholders,
        )

        graph = build_agent_graph(checkpointer or MemorySaver())
        config = {"configurable": {"ctx": ctx, "thread_id": run_id}, "recursion_limit": 40}

        final: dict[str, Any] | None = None
        try:
            final = await asyncio.wait_for(
                graph.ainvoke(initial_state(uid), config), timeout=TOTAL_TIMEOUT_SECONDS
            )
        except asyncio.TimeoutError:
            logger.warning("agent.timeout", extra={"extra_fields": {"user_id": str(uid)}})

        draft, node_path, score, refine_loops, fallback_used = _resolve_result(final, ctx)
        recommendation = await _persist(
            session_factory,
            uid,
            run_id,
            trigger_reason,
            draft,
            node_path,
            score,
            refine_loops,
            fallback_used,
            collector,
            mono_start,
            ctx,
        )
        logger.info(
            "agent.run_done",
            extra={
                "extra_fields": {
                    "user_id": str(uid),
                    "status": recommendation.status,
                    "items": len(recommendation.items),
                    "node_path": node_path,
                    "retrieval_score": score,
                    "refine_loops": refine_loops,
                    "cost_usd": recommendation.cost_usd,
                    "latency_ms": recommendation.latency_ms,
                }
            },
        )
        return recommendation
    finally:
        if own_store:
            await store.close()
