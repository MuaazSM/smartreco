"""Node 2 — ``plan_queries`` (cheap model, structured). PRD §6.4.

Turns the behavioral summary into 2-3 short *retrieval queries* (not recommendations) plus a metadata
filter proposal, via the single Mesh gateway on the ``cheap`` free tier (``model_router`` owns the
model id — never hardcoded here). Under ``MESH_DISABLED`` this returns the ``plan_queries`` fixture.
If the model returns nothing usable, a deterministic profile-derived query set keeps retrieval alive.
"""

from __future__ import annotations

from typing import Any

from app.agent import prompts
from app.agent.context import (
    catalog_facets,
    fallback_queries,
    filters_to_dict,
    get_ctx,
)
from app.agent.schemas import PlanQueriesOutput
from app.llm import mesh


async def plan_queries(state: dict[str, Any], config: Any) -> dict[str, Any]:
    """Behavior → 2-3 retrieval queries + a filter proposal (cheap structured Mesh call)."""
    ctx = get_ctx(config)
    profile = state.get("profile", {})
    messages = prompts.build_plan_messages(profile, state.get("evidence", []), catalog_facets(ctx))
    result = await mesh.complete_structured(
        "plan_queries", messages, PlanQueriesOutput, on_usage=ctx.on_usage
    )
    plan = result.data
    queries = [q.strip() for q in plan.queries if q.strip()][:3] or fallback_queries(profile)
    return {
        "queries": queries,
        "filters": filters_to_dict(plan.filters),
        "node_path": ["plan_queries"],
    }
