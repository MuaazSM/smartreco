"""Versioned prompt templates for the LangGraph nodes (PRD §6.4; §6.5.1 rule 2 — response caching).

Prompt design is a cost lever, not just wording. Mesh offers a free response cache plus provider-side
prefix caching (PRD §6.5.1), so each builder keeps a **stable** system prompt (fixed instructions +
JSON contract) and puts only the varying material in the user message — and within a single run the
catalog block is identical across a ``generate`` retry (only the validation-error note is appended),
so the long prefix stays cacheable exactly when it matters.

``PROMPT_VERSION`` tags the set; bump it on any behavioral change so telemetry can attribute a shift in
output quality to a prompt change. No AI client is imported here — these are pure string builders.
"""

from __future__ import annotations

from typing import Any

PROMPT_VERSION = "v1"

# --------------------------------------------------------------------------------------------------
# Stable system prompts (the cacheable prefix — do not vary per request)
# --------------------------------------------------------------------------------------------------
_PLAN_SYSTEM = (
    "You are the query-planning step of a course-recommendation agent. Turn a learner's recent "
    "behavior into 2-3 SHORT retrieval queries (search phrases, NOT recommendations) that would "
    "surface courses matching their demonstrated interest, plus a metadata filter proposal. "
    "Output JSON only, no prose: "
    '{"queries": [str, ...], "filters": {"category": str|null, "level": str|null, '
    '"max_price_cents": int|null}, "rationale": str}. '
    "Only propose a category/level that appears in the provided catalog facets; leave a filter null "
    "when unsure — an over-narrow filter hurts recall."
)

_GRADE_SYSTEM = (
    "You are the retrieval-grading step of a course-recommendation agent. Given the planned queries "
    "and the retrieved candidate courses, judge how well the candidates actually serve the learner's "
    "demonstrated interest. Output JSON only, no prose: "
    '{"score": float (0..1), "gap": str, "decisions": [{"product_id": str, "keep": bool}, ...]}. '
    "score>=0.7 means the set is good enough to recommend from; below that, describe the gap so the "
    "queries can be refined. Only reference product_ids from the provided candidates."
)

_REFINE_SYSTEM = (
    "You are the query-refinement step of a course-recommendation agent. The previous retrieval was "
    "graded insufficient. Rewrite the queries to close the described gap — widen or narrow the "
    "metadata filters as appropriate — and return 2-3 improved queries. Output JSON only, same shape "
    "as the planner: "
    '{"queries": [str, ...], "filters": {"category": str|null, "level": str|null, '
    '"max_price_cents": int|null}, "rationale": str}.'
)

_GENERATE_SYSTEM = (
    "You are the recommendation writer of a course-recommendation agent. Write a persuasive, "
    "PERSONALIZED recommendation grounded ONLY in the candidate courses provided. Rules: "
    "(1) recommend 3 to 5 items; (2) every item's product_id MUST be copied verbatim from the "
    "candidate list — never invent an id; (3) each item's reason must tie ONE course to ONE thing the "
    "learner actually did (a search they ran, a course/category they engaged with); (4) the narrative "
    "must reference their real behavior, not generic marketing; (5) do NOT state any price or numeric "
    "claim that is not in the candidate metadata. Output JSON only, no prose outside it: "
    '{"headline": str, "narrative": str, "items": [{"product_id": str, "reason": str}, ...]}.'
)


# --------------------------------------------------------------------------------------------------
# Shared formatting helpers (deterministic → stable cacheable text)
# --------------------------------------------------------------------------------------------------
def _fmt_price(price_cents: int) -> str:
    return f"${price_cents / 100:.0f}"


def _profile_block(profile: dict[str, Any]) -> str:
    cats = ", ".join(
        f"{name} ({weight})" for name, weight in (profile.get("top_categories") or {}).items()
    ) or "(none yet)"
    terms = ", ".join((profile.get("top_terms") or {}).keys()) or "(none yet)"
    levels = ", ".join(
        f"{name} ({weight})" for name, weight in (profile.get("level_affinity") or {}).items()
    ) or "(none yet)"
    return (
        f"Top categories: {cats}\n"
        f"Search terms: {terms}\n"
        f"Level affinity: {levels}\n"
        f"Courses already engaged with: {profile.get('seen_count', 0)}"
    )


def _evidence_block(evidence: list[dict[str, Any]]) -> str:
    if not evidence:
        return "Recent behavior: (no recent activity)"
    lines = ["Recent behavior:"]
    for event in evidence:
        if event.get("type") == "search":
            lines.append(f'- searched "{event.get("query", "")}"')
        else:
            title = event.get("title") or event.get("product_id", "")
            category = event.get("category", "")
            suffix = f" ({category})" if category else ""
            lines.append(f'- {event.get("type", "viewed")} "{title}"{suffix}')
    return "\n".join(lines)


def _candidate_block(candidates: list[dict[str, Any]]) -> str:
    if not candidates:
        return "Candidate courses: (none)"
    lines = ["Candidate courses (recommend only from these; copy product_id exactly):"]
    for cand in candidates:
        tags = ", ".join(cand.get("tags") or [])
        lines.append(
            f"- product_id={cand['product_id']} | {cand.get('title', '')} "
            f"| {cand.get('category', '')} | {cand.get('level', '')} "
            f"| {_fmt_price(cand.get('price_cents', 0))} | tags: {tags}"
        )
    return "\n".join(lines)


def _facets_block(facets: dict[str, list[str]]) -> str:
    categories = ", ".join(facets.get("categories") or []) or "(none)"
    levels = ", ".join(facets.get("levels") or []) or "(none)"
    return f"Catalog facets — categories: {categories}\nCatalog facets — levels: {levels}"


# --------------------------------------------------------------------------------------------------
# Message builders (system + user; the caller passes these straight to mesh.complete_structured)
# --------------------------------------------------------------------------------------------------
def build_plan_messages(
    profile: dict[str, Any], evidence: list[dict[str, Any]], facets: dict[str, list[str]]
) -> list[dict[str, str]]:
    user = "\n\n".join(
        [_profile_block(profile), _evidence_block(evidence), _facets_block(facets),
         "Produce the query plan JSON now."]
    )
    return [{"role": "system", "content": _PLAN_SYSTEM}, {"role": "user", "content": user}]


def build_grade_messages(
    profile: dict[str, Any], queries: list[str], candidates: list[dict[str, Any]]
) -> list[dict[str, str]]:
    query_text = "\n".join(f"- {q}" for q in queries) or "- (none)"
    user = "\n\n".join(
        [_profile_block(profile), f"Planned queries:\n{query_text}", _candidate_block(candidates),
         "Produce the grading JSON now."]
    )
    return [{"role": "system", "content": _GRADE_SYSTEM}, {"role": "user", "content": user}]


def build_refine_messages(
    profile: dict[str, Any], queries: list[str], gap: str, facets: dict[str, list[str]]
) -> list[dict[str, str]]:
    query_text = "\n".join(f"- {q}" for q in queries) or "- (none)"
    user = "\n\n".join(
        [_profile_block(profile), f"Previous queries:\n{query_text}",
         f"Grader's gap description: {gap or '(unspecified)'}", _facets_block(facets),
         "Produce the improved query plan JSON now."]
    )
    return [{"role": "system", "content": _REFINE_SYSTEM}, {"role": "user", "content": user}]


def build_generate_messages(
    profile: dict[str, Any],
    evidence: list[dict[str, Any]],
    candidates: list[dict[str, Any]],
    validation_errors: list[str],
) -> list[dict[str, str]]:
    # Catalog block first (the long, stable-within-run prefix), then behavior, then any retry note.
    sections = [_candidate_block(candidates), _profile_block(profile), _evidence_block(evidence)]
    if validation_errors:
        sections.append(
            "The previous attempt was rejected by the grounding gate for these reasons — fix them "
            "and recommend ONLY from the candidate list above:\n"
            + "\n".join(f"- {err}" for err in validation_errors)
        )
    sections.append("Write the recommendation JSON now.")
    user = "\n\n".join(sections)
    return [{"role": "system", "content": _GENERATE_SYSTEM}, {"role": "user", "content": user}]
