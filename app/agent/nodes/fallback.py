"""Deterministic fallback (NO LLM). PRD §6.4: the second-validation-failure safety net.

When ``generate`` fails the grounding gate twice, the graph routes here instead of ever surfacing an
ungrounded item (invariant #2). This builds a recommendation from the highest-ranked *retrieved*
candidates — which are grounded by construction (they came out of ``retrieve``, so they are in the
candidate set and active in Postgres) — with a templated narrative and per-item reasons derived from
the learner's top categories/terms. No model call is made.

``build_fallback_draft`` is also reused by ``run_agent`` for the wall-clock-timeout path, where it is
handed corpus-derived pseudo-candidates so a grounded result is always returned even if the graph was
cancelled mid-run.
"""

from __future__ import annotations

from typing import Any

from app.agent.context import AgentContext, get_ctx

_MIN_ITEMS = 3
_MAX_ITEMS = 5


def _reason(candidate: dict[str, Any], profile: dict[str, Any]) -> str:
    category = candidate.get("category", "")
    level = candidate.get("level", "")
    top_terms = list((profile.get("top_terms") or {}).keys())
    tags = [t.lower() for t in candidate.get("tags", [])]
    hook = next((term for term in top_terms if term.lower() in tags), None)
    base = f"Matches your focus on {category}" if category else "A strong match for your interests"
    if hook:
        base += f" and your interest in {hook}"
    if level:
        base += f"; a {level} course"
    return base + "."


def _headline(profile: dict[str, Any]) -> str:
    categories = list((profile.get("top_categories") or {}).keys())
    if categories:
        return f"More in {categories[0]}, picked for you"
    return "Courses picked for you"


def _narrative(profile: dict[str, Any], count: int) -> str:
    categories = list((profile.get("top_categories") or {}).keys())[:2]
    seen = profile.get("seen_count", 0)
    focus = " and ".join(categories) if categories else "the topics you have been exploring"
    return (
        f"Based on your recent activity across {seen} courses, these {count} picks stay close to "
        f"{focus} — chosen for relevance and variety so there is a clear next step to take."
    )


def build_fallback_draft(
    ctx: AgentContext, profile: dict[str, Any], candidates: list[dict[str, Any]]
) -> dict[str, Any]:
    """A grounded recommendation from the top retrieved candidates + a templated narrative.

    If fewer than three candidates are available, tops up from the active catalog by category match to
    the profile (still grounded — every id references an active row), so the 3-5 item guarantee holds.
    """
    chosen = list(candidates[:_MAX_ITEMS])
    if len(chosen) < _MIN_ITEMS:
        have = {c["product_id"] for c in chosen}
        top_categories = list((profile.get("top_categories") or {}).keys())
        ranked = sorted(
            (doc for doc in ctx.corpus if doc.product_id not in have and doc.product_id not in ctx.seen_product_ids),
            key=lambda doc: (0 if doc.category in top_categories else 1, doc.title),
        )
        for doc in ranked:
            if len(chosen) >= _MAX_ITEMS:
                break
            chosen.append(
                {
                    "product_id": doc.product_id,
                    "title": doc.title,
                    "category": doc.category,
                    "level": doc.level,
                    "price_cents": doc.price_cents,
                    "tags": list(doc.tags),
                }
            )

    items = [{"product_id": c["product_id"], "reason": _reason(c, profile)} for c in chosen]
    return {
        "headline": _headline(profile),
        "narrative": _narrative(profile, len(items)),
        "items": items,
    }


async def fallback(state: dict[str, Any], config: Any) -> dict[str, Any]:
    """Deterministic top-K fallback — grounded by construction, never a raw-LLM passthrough."""
    ctx = get_ctx(config)
    draft = build_fallback_draft(ctx, state.get("profile", {}), state.get("candidates") or [])
    return {
        "draft": draft,
        "fallback_used": True,
        "valid": True,
        "validation_errors": [],
        "node_path": ["fallback"],
    }
