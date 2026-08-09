"""The SmartReco LangGraph recommendation agent (PRD §6.4).

Public surface for Phases 7 (trigger/cache) and 8 (dashboard):

    from app.agent import run_agent, Recommendation
    rec = await run_agent(user_id, trigger_reason="event")

``run_agent`` runs the seven-node graph, persists the result (as the user's single ``is_current``
recommendation) plus its ``agent_runs`` telemetry, and returns a grounded ``Recommendation``.
"""

from __future__ import annotations

from app.agent.graph import build_agent_graph, initial_state, run_agent
from app.agent.schemas import Recommendation, RecommendationItem

__all__ = [
    "run_agent",
    "build_agent_graph",
    "initial_state",
    "Recommendation",
    "RecommendationItem",
]
