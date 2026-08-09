"""Observability metrics (PRD §6.8 / A3, F8 bonus; IMPLEMENTATION.md Phase 9b).

Pure DB reads over ``agent_runs`` + ``events`` — no LLM call, no agent run of its own, no new
dependency. Feeds ``GET /api/admin/metrics`` (`app/api/routes/admin.py`).

Three judged numbers:
  * ``llm_calls_per_100_events`` — how many **full** graph runs (``cache_hit IS NULL``, i.e. a real
    Mesh call happened) landed per 100 tracked events. Low is good: it is the visible proof that the
    trigger policy (``app/services/trigger.py``) is doing its job of *not* calling the LLM on every
    event (CLAUDE.md invariant #6), rather than a claim a judge has to take on faith.
  * ``cache_hit_rate`` — the share of ``agent_runs`` rows served out of the L1 (exact profile-hash)
    or L2 (semantic) cache instead of a full generation.
  * ``cost`` — total and median ``cost_usd``, straight from the Mesh-metered per-call accounting
    already written to ``agent_runs`` by ``app/agent/graph.py``.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import AgentRun, Event


@dataclass(frozen=True)
class MetricsResult:
    llm_calls_per_100_events: float
    cache_hit_rate: float
    cost_total_usd: float
    cost_median_usd: float
    total_events: int
    total_runs: int
    full_runs: int
    l1_hits: int
    l2_hits: int


async def compute_metrics(db: AsyncSession) -> MetricsResult:
    """Compute the admin-console health numbers in a handful of cheap aggregate queries."""
    total_events = int(await db.scalar(select(func.count()).select_from(Event)) or 0)
    total_runs = int(await db.scalar(select(func.count()).select_from(AgentRun)) or 0)
    full_runs = int(
        await db.scalar(
            select(func.count()).select_from(AgentRun).where(AgentRun.cache_hit.is_(None))
        )
        or 0
    )
    l1_hits = int(
        await db.scalar(
            select(func.count()).select_from(AgentRun).where(AgentRun.cache_hit == "l1")
        )
        or 0
    )
    l2_hits = int(
        await db.scalar(
            select(func.count()).select_from(AgentRun).where(AgentRun.cache_hit == "l2")
        )
        or 0
    )

    # Pulled client-side and reduced with `statistics` rather than a Postgres `percentile_cont`
    # ordered-set aggregate: agent_runs is small (a hackathon submission's run history, not a
    # production-scale table) and this keeps the query trivially dialect-portable.
    cost_rows = (await db.execute(select(AgentRun.cost_usd))).scalars().all()
    costs = [float(c) for c in cost_rows]

    llm_calls_per_100_events = (full_runs / total_events * 100) if total_events > 0 else 0.0
    cache_hit_rate = ((l1_hits + l2_hits) / total_runs) if total_runs > 0 else 0.0

    return MetricsResult(
        llm_calls_per_100_events=round(llm_calls_per_100_events, 4),
        cache_hit_rate=round(cache_hit_rate, 4),
        cost_total_usd=round(sum(costs), 6),
        cost_median_usd=round(statistics.median(costs), 6) if costs else 0.0,
        total_events=total_events,
        total_runs=total_runs,
        full_runs=full_runs,
        l1_hits=l1_hits,
        l2_hits=l2_hits,
    )
