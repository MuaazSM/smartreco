"""Node 6 — ``generate`` (quality model, structured). PRD §6.4.

Writes the persuasive, personalized recommendation — ``{headline, narrative, items:[{product_id,
reason}]}`` — grounded only in the retrieved candidates, via the single Mesh gateway on the ``quality``
tier (free large model → paid quality fallback; ``model_router`` owns the choice). The narrative must
reference actual behavior and each item's reason ties one course to one observed signal (enforced by
the stable system prompt in ``app/agent/prompts``).

On a validation retry, the prior gate's ``validation_errors`` are injected into the prompt so the model
can correct itself; ``generate_attempts`` is bumped and hard-capped at 2 (initial + one retry) by the
graph. If the run is past its deadline, the node skips the Mesh call and leaves ``draft`` empty so the
graph routes straight to the deterministic fallback. Under ``MESH_DISABLED`` the fixture's sentinel ids
are remapped onto real candidates (offline convenience) — but the grounding gate remains authoritative.
"""

from __future__ import annotations

from typing import Any

from app.agent import prompts
from app.agent.context import get_ctx, over_deadline, remap_placeholders
from app.agent.schemas import GenerateOutput
from app.llm import mesh


async def generate(state: dict[str, Any], config: Any) -> dict[str, Any]:
    """Produce the grounded recommendation draft (quality structured Mesh call)."""
    ctx = get_ctx(config)
    attempts = state.get("generate_attempts", 0) + 1
    candidates = state.get("candidates") or []

    if over_deadline(ctx):  # deadline guard → empty draft → validate routes to fallback
        return {"draft": None, "generate_attempts": attempts, "node_path": ["generate"]}

    messages = prompts.build_generate_messages(
        state.get("profile", {}),
        state.get("evidence", []),
        candidates,
        state.get("validation_errors") or [],
    )
    result = await mesh.complete_structured(
        "generate", messages, GenerateOutput, temperature=0.4, on_usage=ctx.on_usage
    )
    draft_out = result.data
    items = [{"product_id": item.product_id, "reason": item.reason} for item in draft_out.items]
    if ctx.remap_placeholders:
        items = remap_placeholders(items, [c["product_id"] for c in candidates])

    draft = {"headline": draft_out.headline, "narrative": draft_out.narrative, "items": items}
    return {"draft": draft, "generate_attempts": attempts, "node_path": ["generate"]}
