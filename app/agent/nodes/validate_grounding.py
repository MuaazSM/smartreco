"""Node 7 — ``validate_grounding`` (NO LLM). PRD §6.4 — THE HARD GATE (invariant #2).

The structural gate the whole submission's grounding claim rests on: the system must **never** show an
ungrounded recommendation. This node validates the ``generate`` draft with zero model calls and either
passes a clean, grounded draft through or rejects it so the graph retries (once) and then falls back to
the deterministic top-K path (which is grounded by construction). There is deliberately **no** path
that returns raw LLM output around this gate.

Checks (any failure rejects the whole draft — we do not silently drop bad items):
  * every ``product_id`` is in the retrieved candidate set **and** active in Postgres (== the corpus);
  * no duplicate ids;
  * 3-5 items (a draft with >5 grounded items is trimmed to 5 rather than rejected);
  * no price/numeric claim in the narrative that is absent from the candidate metadata.

On rejection it records ``validation_errors`` (injected into the retry prompt) and leaves the routing
to the graph; ``generate_attempts`` (bumped by ``generate``) caps the retries at one.
"""

from __future__ import annotations

import re
from typing import Any

from app.agent.context import get_ctx

_MIN_ITEMS = 3
_MAX_ITEMS = 5
# Dollar amounts the narrative might assert, e.g. "$49" or "$1,299.00".
_PRICE_RE = re.compile(r"\$\s?(\d[\d,]*)(?:\.(\d{2}))?")


def _allowed_price_dollars(candidates: list[dict[str, Any]]) -> set[int]:
    """Whole-dollar prices present in the candidate metadata (the only prices a narrative may cite)."""
    return {int(round(c.get("price_cents", 0) / 100)) for c in candidates}


def _price_claim_errors(narrative: str, candidates: list[dict[str, Any]]) -> list[str]:
    allowed = _allowed_price_dollars(candidates)
    errors: list[str] = []
    for whole, _cents in _PRICE_RE.findall(narrative or ""):
        dollars = int(whole.replace(",", ""))
        if dollars not in allowed:
            errors.append(f"narrative cites price ${dollars} not present in candidate metadata")
    return errors


def _validate(draft: dict[str, Any] | None, candidates: list[dict[str, Any]], active_ids: set[str]):
    """Return ``(errors, grounded_items)``. Empty errors + 3-5 grounded items == valid."""
    errors: list[str] = []
    if not draft:
        return ["generate produced no draft"], []

    candidate_ids = {c["product_id"] for c in candidates}
    seen: set[str] = set()
    grounded: list[dict[str, Any]] = []
    for item in draft.get("items", []):
        pid = str(item.get("product_id", ""))
        if pid in seen:
            errors.append(f"duplicate item {pid}")
            continue
        if pid not in candidate_ids:
            errors.append(f"item {pid} is not in the retrieved candidate set")
            continue
        if pid not in active_ids:
            errors.append(f"item {pid} is not an active catalog product")
            continue
        seen.add(pid)
        grounded.append(item)

    errors.extend(_price_claim_errors(draft.get("narrative", ""), candidates))

    if len(grounded) < _MIN_ITEMS:
        errors.append(f"only {len(grounded)} grounded items (need at least {_MIN_ITEMS})")

    return errors, grounded[:_MAX_ITEMS]


async def validate_grounding(state: dict[str, Any], config: Any) -> dict[str, Any]:
    """The structural grounding gate. Passes a clean draft or flags it for retry/fallback."""
    ctx = get_ctx(config)
    candidates = state.get("candidates") or []
    errors, grounded = _validate(state.get("draft"), candidates, ctx.active_ids)

    if errors:
        return {"valid": False, "validation_errors": errors, "node_path": ["validate_grounding"]}

    clean_draft = {
        "headline": state["draft"].get("headline", ""),
        "narrative": state["draft"].get("narrative", ""),
        "items": grounded,
    }
    return {
        "valid": True,
        "draft": clean_draft,
        "validation_errors": [],
        "node_path": ["validate_grounding"],
    }
