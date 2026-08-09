"""Optional pairwise reranker for retrieval candidates (PRD §6.4 retrieval polish; Phase 6 bonus).

Reranking is **off by default** — ``rerank`` is a pass-through so the graph runs at near-zero cost
(PRD §6.5.1) and tests are deterministic. When ``use_llm=True`` the cheap ``rerank`` node scores the
candidates through the single Mesh gateway (``app.llm.mesh``, invariants #1/#7) using the ``rerank``
structured schema/fixture — no prose, just per-candidate scores — and reorders by that score, leaving
any candidate the model did not score in its original (already MMR-diversified) position.

No AI client is constructed here; the only model access is ``mesh.complete_structured`` (routed to the
free ``cheap`` tier by ``model_router``). This keeps the reranker a thin, swappable polish layer over
``hybrid_retriever`` — never a second retrieval path.
"""

from __future__ import annotations

from app.core.logging import get_logger
from app.llm import mesh
from app.vector.hybrid_retriever import Candidate

logger = get_logger(__name__)

_RERANK_SYSTEM = (
    "You are a retrieval reranker for an online course catalog. Given a learner's search queries and "
    "a list of candidate courses, score each candidate from 0.0 to 1.0 for how well it matches the "
    "queries. Output JSON only: {\"ranking\": [{\"product_id\": str, \"score\": float}, ...]}. "
    "No prose, no new ids — score only the candidates provided."
)


def _rerank_user_message(queries: list[str], candidates: list[Candidate]) -> str:
    lines = ["Queries:"]
    lines += [f"- {q}" for q in queries] or ["- (none)"]
    lines.append("")
    lines.append("Candidates:")
    for cand in candidates:
        lines.append(
            f"- id={cand.product_id} | {cand.title} | {cand.category} | {cand.level}"
        )
    lines.append("")
    lines.append("Return the ranking JSON now.")
    return "\n".join(lines)


async def rerank(
    queries: list[str],
    candidates: list[Candidate],
    *,
    use_llm: bool = False,
    on_usage: object | None = None,
) -> list[Candidate]:
    """Return ``candidates`` reordered by rerank score, or unchanged when ``use_llm`` is false.

    Pass-through by default (the documented Phase-6 starting point). With ``use_llm=True`` it makes one
    cheap Mesh ``rerank`` call and reorders; unscored candidates keep their relative order after the
    scored ones. Any Mesh error degrades gracefully to the input order — reranking is polish, never a
    correctness dependency.
    """
    if not use_llm or not candidates:
        return candidates
    from app.agent.schemas import RerankOutput  # local import: avoids agent<->vector import cycle

    messages = [
        {"role": "system", "content": _RERANK_SYSTEM},
        {"role": "user", "content": _rerank_user_message(queries, candidates)},
    ]
    try:
        result = await mesh.complete_structured(
            "rerank", messages, RerankOutput, on_usage=on_usage  # type: ignore[arg-type]
        )
    except Exception as exc:  # pragma: no cover - degrade to input order on any Mesh failure
        logger.warning("agent.rerank_failed", extra={"extra_fields": {"error": str(exc)}})
        return candidates

    scores = {entry.product_id: entry.score for entry in result.data.ranking}
    scored = [c for c in candidates if c.product_id in scores]
    unscored = [c for c in candidates if c.product_id not in scores]
    scored.sort(key=lambda c: scores.get(c.product_id, 0.0), reverse=True)
    return scored + unscored
