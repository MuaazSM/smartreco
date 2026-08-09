"""Node 1 — ``build_profile`` (NO LLM). PRD §6.4: the cheap, deterministic backbone.

Assembles the compact behavioral summary the rest of the graph reasons over: weighted top categories,
search terms, level affinity, and how many courses the learner has already engaged with — plus the
recent-event ``evidence`` the ``generate`` node ties each recommendation back to. The heavy lifting
(the decayed interest centroid + the recent-event query) already happened in ``run_agent``'s prep and
lives on ``ctx``; this node shapes it into state. It makes no Mesh call of any kind (invariant #6/#7).
"""

from __future__ import annotations

from typing import Any

from app.agent.context import AgentContext, get_ctx


def _summarize(ctx: AgentContext) -> dict[str, Any]:
    snapshot = ctx.profile_snapshot
    return {
        "top_categories": dict(list(snapshot.top_categories.items())[:6]),
        "top_terms": dict(list(snapshot.top_terms.items())[:10]),
        "level_affinity": dict(snapshot.level_affinity),
        "seen_count": len(ctx.seen_product_ids),
        "considered_events": snapshot.considered_events,
        "interest_vector_dim": len(ctx.interest_vector),
    }


async def build_profile(state: dict[str, Any], config: Any) -> dict[str, Any]:
    """Produce ``profile`` + ``evidence`` and reset the per-run counters. Deterministic, LLM-free."""
    ctx = get_ctx(config)
    return {
        "profile": _summarize(ctx),
        "evidence": list(ctx.recent_events),
        "refine_loops": 0,
        "generate_attempts": 0,
        "validation_errors": [],
        "candidates": [],
        "node_path": ["build_profile"],
    }
