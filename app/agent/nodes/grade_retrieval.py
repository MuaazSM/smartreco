"""Node 4 — ``grade_retrieval`` (cheap model, structured). PRD §6.4.

Self-grades the retrieved candidates 0-1 (do they actually serve the learner's demonstrated interest?),
describes the gap, and returns per-candidate keep/drop decisions — all via the single Mesh gateway on
the ``cheap`` free tier. The graph's conditional edge reads ``retrieval_score``: ≥0.7 → generate; below
that with refine budget left → refine; budget exhausted → generate anyway (see ``graph.py``).

Keep/drop is applied defensively: a decision is honored only when its ``product_id`` matches a real
candidate, and the candidate set is never trimmed below three — so the ``MESH_DISABLED`` fixture (whose
decisions reference sentinel ids) simply leaves the real candidates intact rather than emptying them.
"""

from __future__ import annotations

from typing import Any

from app.agent import prompts
from app.agent.context import get_ctx
from app.agent.schemas import GradeOutput
from app.llm import mesh

_MIN_KEEP = 3


def _apply_decisions(candidates: list[dict[str, Any]], grade: GradeOutput) -> list[dict[str, Any]]:
    drop_ids = {d.product_id for d in grade.decisions if not d.keep}
    keep_ids = {d.product_id for d in grade.decisions if d.keep}
    candidate_ids = {c["product_id"] for c in candidates}
    if not (drop_ids | keep_ids) & candidate_ids:
        return candidates  # decisions reference nothing real (e.g. offline fixture) → keep all
    effective_drop = drop_ids - keep_ids
    filtered = [c for c in candidates if c["product_id"] not in effective_drop]
    return filtered if len(filtered) >= _MIN_KEEP else candidates


async def grade_retrieval(state: dict[str, Any], config: Any) -> dict[str, Any]:
    """Score the candidate set 0-1 + gap + keep/drop (cheap structured Mesh call)."""
    ctx = get_ctx(config)
    candidates = state.get("candidates") or []
    messages = prompts.build_grade_messages(
        state.get("profile", {}), state.get("queries", []), candidates
    )
    result = await mesh.complete_structured(
        "grade_retrieval", messages, GradeOutput, on_usage=ctx.on_usage
    )
    grade = result.data
    return {
        "retrieval_score": grade.score,
        "retrieval_gap": grade.gap,
        "candidates": _apply_decisions(candidates, grade),
        "node_path": ["grade_retrieval"],
    }
