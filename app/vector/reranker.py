"""Optional pairwise reranker for retrieval candidates (PRD §6.4 retrieval polish; Phase 6/9d bonus).

Reranking is **off by default** — the ``retrieve`` node only calls ``rerank(..., use_llm=True)`` when
``ctx.enable_rerank`` is set (``run_agent(..., enable_rerank=True)``), so the graph runs at near-zero
cost by default (PRD §6.5.1). When enabled, this module makes **one** cheap Mesh call through the
single gateway (``app.llm.mesh``, invariants #1/#7) — no prose, just a per-candidate score — and
reorders the MMR-diversified candidates by that score. It is a pure re-ordering: it never drops a
candidate and never introduces a ``product_id`` the retriever did not already surface, so it can never
weaken the downstream grounding gate. Any candidate the model did not score keeps its original
(already MMR-diversified) relative position, appended after the scored ones.

No AI client is constructed here; the only model access is ``mesh.complete_structured`` (routed to the
free ``cheap`` tier by ``model_router``). This keeps the reranker a thin, swappable polish layer over
``hybrid_retriever`` — never a second retrieval path.

Honesty note on "measured lift" (PRD §9d): ``measure_rerank_lift`` below reports a *structural*
before/after ordering delta (how many candidates moved, top-k overlap, Kendall's tau) — it is
deliberately NOT a semantic quality score. Under ``MESH_DISABLED=true`` both the candidate vectors
(``app.llm.mesh._pseudo_embedding``) and the rerank fixture are synthetic/seeded, so any ordering
change offline is an artifact of the fixture, not evidence of retrieval quality. A real, meaningful
lift number requires a live ``MESH_API_KEY`` and real (re-seeded) product embeddings — see
``tests/test_reranker.py`` and the phase report for how to read the offline numbers honestly.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

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

# Matches the MESH_DISABLED fixture's sentinel ids (app/llm/fixtures/rerank.json). Mirrors
# app.agent.context.remap_placeholders: an *offline convenience* that maps the fixture's fixed
# "PLACEHOLDER_PRODUCT_<n>" ids onto the n-th actual candidate so the offline fixture path exercises a
# real reordering instead of silently no-op'ing (the fixture's literal ids never match a real catalog
# product_id, so without this every offline rerank call would score zero candidates and be a no-op
# pass-through in practice). It NEVER invents an id outside the input candidates: the remap target is
# always ``candidates[index].product_id``, one of the very ids being reranked. A live model response
# that happens to *not* match this sentinel pattern is left as-is and simply ignored if it isn't one of
# the input candidate ids (the same safety property ``rerank`` already has).
_PLACEHOLDER_RE = re.compile(r"^PLACEHOLDER_PRODUCT_(\d+)$")


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


def _score_map(ranking: list, candidates: list[Candidate]) -> dict[str, float]:
    """Build ``{product_id: score}`` from the model's ranking, remapping fixture sentinel ids.

    ``ranking`` entries are ``RerankEntry``-shaped (``product_id``, ``score``). Any id matching the
    ``PLACEHOLDER_PRODUCT_<n>`` sentinel is mapped onto the n-th input candidate's real id — the only
    substitution this function ever performs, and it always resolves to an id already present in
    ``candidates``. Entries that reference neither a real candidate id nor a resolvable placeholder are
    kept verbatim; the caller ignores any key that isn't an input candidate's id, so they cannot
    resurrect a dropped/invented product.
    """
    scores: dict[str, float] = {}
    for entry in ranking:
        pid = entry.product_id
        match = _PLACEHOLDER_RE.match(pid)
        if match:
            index = int(match.group(1)) - 1
            if 0 <= index < len(candidates):
                pid = candidates[index].product_id
        scores[pid] = entry.score
    return scores


async def rerank(
    queries: list[str],
    candidates: list[Candidate],
    *,
    use_llm: bool = False,
    on_usage: object | None = None,
) -> list[Candidate]:
    """Return ``candidates`` reordered by rerank score, or unchanged when ``use_llm`` is false.

    Pass-through by default (the documented Phase-6 starting point / the retriever's default when
    ``ctx.enable_rerank`` is off). With ``use_llm=True`` it makes one cheap Mesh ``rerank`` call and
    reorders; unscored candidates keep their relative order after the scored ones. Any Mesh error
    (network, timeout, malformed structured output) degrades gracefully to the input order — reranking
    is polish, never a correctness dependency the graph can fail on.

    This is always a **pure permutation** of ``candidates``: the returned list has exactly the same
    ``product_id`` set and length as the input, just reordered. No id is ever added or dropped, which
    is what keeps a reranked candidate list safe to feed straight into ``grade_retrieval``/``generate``
    without weakening the grounding gate (invariant #2).
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

    scores = _score_map(result.data.ranking, candidates)
    scored = [c for c in candidates if c.product_id in scores]
    unscored = [c for c in candidates if c.product_id not in scores]
    scored.sort(key=lambda c: scores.get(c.product_id, 0.0), reverse=True)
    reordered = scored + unscored
    logger.info(
        "agent.rerank",
        extra={
            "extra_fields": {
                "candidates": len(candidates),
                "scored": len(scored),
                "unscored": len(unscored),
            }
        },
    )
    return reordered


# --------------------------------------------------------------------------------------------------
# Measured lift (PRD §9d "enable the reranker with measured lift")
# --------------------------------------------------------------------------------------------------
@dataclass(slots=True)
class RerankLiftReport:
    """A **structural** before/after comparison of a candidate ordering — never a quality score.

    ``moved`` counts positions that changed; ``top_k_overlap`` is the fraction of the pre-rerank top-k
    ids still in the post-rerank top-k; ``kendall_tau`` is Kendall's tau-a rank correlation in
    ``[-1, 1]`` (``1.0`` = identical order, ``-1.0`` = fully reversed, ``None`` if fewer than 2
    candidates). None of these numbers assert the reranked order is *better* — only how different it
    is. See the module docstring for why an offline (``MESH_DISABLED``) number is not a real lift.
    """

    baseline_order: list[str]
    reranked_order: list[str]
    moved: int
    top_k_overlap: float
    kendall_tau: float | None
    note: str = field(
        default=(
            "Structural re-ordering delta only, not a semantic-quality lift. MESH_DISABLED uses "
            "seeded pseudo-random vectors and a fixture rerank response, so this number does not "
            "demonstrate retrieval quality improved — only that the mechanism reorders candidates. "
            "Measure real lift with a live MESH_API_KEY and live embeddings."
        )
    )


def _kendall_tau(baseline: list[str], reordered: list[str]) -> float | None:
    """Kendall's tau-a between two permutations of the same items. ``None`` for < 2 items."""
    n = len(baseline)
    if n < 2:
        return None
    rank = {pid: index for index, pid in enumerate(reordered)}
    positions = [rank[pid] for pid in baseline]
    concordant = discordant = 0
    for i in range(n):
        for j in range(i + 1, n):
            if (positions[i] - positions[j]) * (i - j) > 0:
                concordant += 1
            else:
                discordant += 1
    total = concordant + discordant
    return (concordant - discordant) / total if total else None


def measure_rerank_lift(
    baseline: list[Candidate], reranked: list[Candidate]
) -> RerankLiftReport:
    """Compare a rerank-off ordering to a rerank-on ordering of the *same* candidate set.

    Raises ``ValueError`` if the two lists are not a permutation of one another (which would indicate
    a correctness bug — this function is a diagnostic, not a tolerant one). Callers typically produce
    ``baseline`` via ``rerank(..., use_llm=False)`` and ``reranked`` via ``rerank(..., use_llm=True)``
    on the identical input candidates (see ``tests/test_reranker.py``).
    """
    base_ids = [c.product_id for c in baseline]
    new_ids = [c.product_id for c in reranked]
    if set(base_ids) != set(new_ids) or len(base_ids) != len(new_ids):
        raise ValueError(
            "measure_rerank_lift requires baseline/reranked to be permutations of the same "
            "candidate set — got different id sets, which would itself be a grounding-safety bug."
        )
    moved = sum(1 for a, b in zip(base_ids, new_ids) if a != b)
    k = min(5, len(base_ids))
    top_k_overlap = len(set(base_ids[:k]) & set(new_ids[:k])) / k if k else 1.0
    return RerankLiftReport(
        baseline_order=base_ids,
        reranked_order=new_ids,
        moved=moved,
        top_k_overlap=top_k_overlap,
        kendall_tau=_kendall_tau(base_ids, new_ids),
    )
