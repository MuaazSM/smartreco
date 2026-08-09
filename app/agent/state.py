"""The LangGraph shared state (PRD §6.4 ``AgentState``).

A ``TypedDict`` threaded through the seven nodes. Every value is JSON-serializable (plain dicts, lists,
scalars) so any LangGraph checkpointer — the in-memory ``MemorySaver`` used now, or the Postgres
checkpointer wired later — can persist/replay a run without custom serializers.

``node_path`` carries an ``operator.add`` reducer: each node returns ``{"node_path": ["<name>"]}`` and
LangGraph *appends* rather than overwrites, so the final path records the true execution order —
including every refine loop and the fallback. That accumulated path is what ``run_agent`` writes to
``agent_runs.node_path``: the proof the graph is real rather than decorative (CLAUDE.md "The agent").

The PRD's typed fields map here as: ``profile`` (a compact summary dict), ``evidence`` (compact event
dicts), ``candidates`` (retrieved catalog rows as dicts), ``draft`` (the ``generate`` output dict).
Heavy artifacts that would bloat the checkpoint — the 1536-dim interest vector, full product vectors —
live on the (non-serialized) ``AgentContext`` instead, never in state.
"""

from __future__ import annotations

import operator
from typing import Annotated, TypedDict


class AgentState(TypedDict, total=False):
    """Shared state for the recommendation graph. All fields optional so nodes patch incrementally."""

    # --- identity ---
    user_id: str

    # --- build_profile ---
    profile: dict            # compact behavioral summary: top_categories/terms/level_affinity/seen
    evidence: list[dict]     # the specific recent events driving this run (compact)

    # --- plan_queries / refine_queries ---
    queries: list[str]       # planned retrieval queries
    filters: dict            # metadata filter proposal {category, level, max_price_cents}

    # --- retrieve ---
    candidates: list[dict]   # top-12 hybrid-retrieved catalog rows (grounding candidate set)

    # --- grade_retrieval ---
    retrieval_score: float   # 0-1 self-grade
    retrieval_gap: str       # what the grader found missing (drives refine)
    refine_loops: int        # number of refine loops taken (hard cap 2)

    # --- generate ---
    draft: dict | None       # {headline, narrative, items:[{product_id, reason}]}
    generate_attempts: int   # generate calls made (initial + at most one retry)

    # --- validate_grounding / fallback ---
    validation_errors: list[str]
    valid: bool
    fallback_used: bool

    # --- observability: accumulated across every node (operator.add reducer) ---
    node_path: Annotated[list[str], operator.add]
