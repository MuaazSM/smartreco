"""The seven graph nodes (PRD §6.4), one per file, re-exported for ``graph.py`` to wire.

  1. build_profile      (no LLM)   — compact behavioral summary + evidence
  2. plan_queries       (cheap)    — behavior → retrieval queries + filter proposal
  3. retrieve           (no LLM*)  — hybrid dense+BM25 → RRF → filter → MMR → top-12  (*embedding only)
  4. grade_retrieval    (cheap)    — self-grade 0-1 + gap + keep/drop
  5. refine_queries     (cheap)    — rewrite using the gap, loop back to retrieve
  6. generate           (quality)  — persuasive grounded {headline, narrative, items[]}
  7. validate_grounding (no LLM)   — the hard grounding gate
  +  fallback           (no LLM)   — deterministic top-K when generation fails the gate twice
"""

from __future__ import annotations

from app.agent.nodes.build_profile import build_profile
from app.agent.nodes.fallback import build_fallback_draft, fallback
from app.agent.nodes.generate import generate
from app.agent.nodes.grade_retrieval import grade_retrieval
from app.agent.nodes.plan_queries import plan_queries
from app.agent.nodes.refine_queries import refine_queries
from app.agent.nodes.retrieve import retrieve
from app.agent.nodes.validate_grounding import validate_grounding

__all__ = [
    "build_profile",
    "plan_queries",
    "retrieve",
    "grade_retrieval",
    "refine_queries",
    "generate",
    "validate_grounding",
    "fallback",
    "build_fallback_draft",
]
