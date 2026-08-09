"""Node 5 — ``refine_queries`` (cheap model, structured). PRD §6.4 — the visible refine loop.

When the grader scores the retrieval below 0.7 and refine budget remains, this node rewrites the
queries to close the grader's gap (widening or narrowing the metadata filters) and the graph loops
back to ``retrieve``. **This is exactly the loop the brief's bonus asks to make visible** — it appears
in ``node_path`` (and any LangSmith trace) as ``refine_queries`` between two ``retrieve`` entries.

The loop counter is incremented here and is hard-capped at 2 by the graph's conditional edge — an
unbounded refine loop burns quota and hangs the demo (PRD §6.4). If the run is already over its
wall-clock deadline, the node skips the Mesh call and just bumps the counter so the graph settles fast.
Uses the ``refine_queries`` cheap tier / fixture (``app/llm/fixtures/refine_queries.json``).
"""

from __future__ import annotations

from typing import Any

from app.agent import prompts
from app.agent.context import (
    catalog_facets,
    filters_to_dict,
    get_ctx,
    over_deadline,
)
from app.agent.schemas import PlanQueriesOutput
from app.llm import mesh


async def refine_queries(state: dict[str, Any], config: Any) -> dict[str, Any]:
    """Rewrite queries using the grader's gap and loop back to retrieve (cheap structured call)."""
    ctx = get_ctx(config)
    loops = state.get("refine_loops", 0) + 1

    if over_deadline(ctx):  # deadline guard between nodes — settle without another Mesh call
        return {"refine_loops": loops, "node_path": ["refine_queries"]}

    messages = prompts.build_refine_messages(
        state.get("profile", {}),
        state.get("queries", []),
        state.get("retrieval_gap", ""),
        catalog_facets(ctx),
    )
    result = await mesh.complete_structured(
        "refine_queries", messages, PlanQueriesOutput, on_usage=ctx.on_usage
    )
    plan = result.data
    queries = [q.strip() for q in plan.queries if q.strip()][:3] or state.get("queries", [])
    return {
        "queries": queries,
        "filters": filters_to_dict(plan.filters),
        "refine_loops": loops,
        "node_path": ["refine_queries"],
    }
