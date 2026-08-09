"""The cost gate: regenerate recommendations **only when it matters** (PRD §6.5 + Figure 6; F5).

This is the explicitly-judged efficiency feature — target **< 1 LLM generation per 40 events**. The
event-ingest background task (``app/api/routes/events.py``) calls ``maybe_regenerate`` *after* the
batch is persisted and the profile rebuilt, never inside the hot request handler (invariant #6). The
compound gate and the two cache layers in front of a full agent run are what turn a stream of events
into a trickle of LLM calls.

Decision flow (every early exit is a *saved* LLM call, logged with the run id):

    should_regenerate = (events_since_gen ≥ 8 OR drift > 0.15 OR a high-intent event)
                        AND (> 10 min since the last generation)          ← cooldown
                        AND (Redis lock gen:{user_id} acquired)           ← serializes per user

If it fires, ``maybe_regenerate`` walks three caches before spending tokens:

    L1 (exact)    profile_hash unchanged since the last generation → serve the stored recommendation,
                  no LLM at all.
    L2 (semantic) cosine(current interest vector, generation-time vector) > 0.95 → reuse the stored,
                  already-grounded items and re-personalize *only the narrative* with ONE cheap call.
    miss          full seven-node agent run (``app.agent.run_agent``); the L3 embedding content-hash
                  cache inside the Mesh gateway still applies.

"drift since generation" and the L2 cosine both compare against the interest vector captured *at the
last generation* — stashed in Redis by ``_mark_generated`` (Phase 5 left ``last_generated_at`` /
``events_since_gen`` for Phase 7 to own; this module is the only writer that resets them).

The ONLY LLM call this module makes is the L2 narrative re-personalization via ``mesh.complete_text``
(a single cheap-tier call). Everything else — the gate, L1, and the drift math — is Mesh-free
(invariant #1). ``run_agent`` (the miss path) owns all other model access.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import func, select, update

from app.agent import run_agent
from app.core.logging import get_logger, set_run_id
from app.db.models import AgentRun, UserProfile
from app.db.models import Recommendation as RecommendationRow
from app.db.session import AsyncSessionLocal
from app.llm import mesh
from app.services import cache
from app.services.profile import cosine_distance
from app.vector.qdrant_client import VectorStore

logger = get_logger(__name__)

# --- the compound trigger thresholds (PRD §6.5) ---
MIN_EVENTS_SINCE_GEN = 8
DRIFT_THRESHOLD = 0.15
COOLDOWN_SECONDS = 600  # 10 minutes since the last generation
# cosine > 0.95 ⇔ cosine *distance* < 0.05 — the L2 semantic-cache reuse threshold.
L2_COSINE_DISTANCE_MAX = 1.0 - 0.95
# A manual /refresh forces a paid run, so it is throttled per user.
REFRESH_RATE_LIMIT_SECONDS = 15

# A "high-intent" event short-circuits the events/drift arithmetic — an explicit buying signal is
# worth a fresh look on its own. Cart is the weight-3.0 top of the PRD §6.3 weighting table.
HIGH_INTENT_EVENT_TYPES: frozenset[str] = frozenset({"cart"})


def batch_has_high_intent(events) -> bool:
    """True if a just-ingested batch contains a high-intent (cart) event — the OR-arm of the gate."""
    return any(getattr(event, "event_type", None) in HIGH_INTENT_EVENT_TYPES for event in events)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _as_aware(value: datetime) -> datetime:
    """Treat a naive timestamp as UTC (Postgres ``timestamptz`` returns aware; be defensive)."""
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


# --------------------------------------------------------------------------------------------------
# Decision + outcome records
# --------------------------------------------------------------------------------------------------
@dataclass(slots=True)
class TriggerDecision:
    """The gate's verdict. ``lock_token`` is set only when ``should_run`` and the lock was acquired —
    the caller MUST release it (``cache.release_lock``) in a finally."""

    should_run: bool
    reason: str  # the firing arm(s) when should_run, else the early-exit reason
    lock_token: str | None = None


@dataclass(slots=True)
class RegenerationOutcome:
    """What ``maybe_regenerate`` did. ``triggered`` means the gate let us past the lock; ``cache_hit``
    records which layer served it (``'l1'``/``'l2'``/``None`` for a full run); ``ran_full_agent`` is
    the only path that spends a full generation."""

    triggered: bool
    reason: str
    cache_hit: str | None = None
    ran_full_agent: bool = False
    recommendation_id: str | None = None
    run_id: str | None = None


# --------------------------------------------------------------------------------------------------
# The gate (pure except for the lock acquire, which is the last step so we only lock when proceeding)
# --------------------------------------------------------------------------------------------------
async def should_regenerate(
    user_id: uuid.UUID | str,
    *,
    events_since_gen: int,
    drift: float,
    high_intent: bool,
    last_generated_at: datetime | None,
    redis=None,
    now: datetime | None = None,
    acquire: bool = True,
) -> TriggerDecision:
    """Evaluate the compound gate. Threshold and cooldown are checked first (cheap, side-effect-free);
    the Redis lock is acquired *last*, only when we are otherwise going to run — so a rejected trigger
    never needlessly takes (and must release) the lock.

    Returns a ``TriggerDecision``; when ``should_run`` is True and ``acquire`` is True, ``lock_token``
    holds the acquired lock that the caller is responsible for releasing.
    """
    now = now or _utcnow()

    reasons: list[str] = []
    if events_since_gen >= MIN_EVENTS_SINCE_GEN:
        reasons.append("events")
    if drift > DRIFT_THRESHOLD:
        reasons.append("drift")
    if high_intent:
        reasons.append("high_intent")
    if not reasons:
        return TriggerDecision(False, "below_threshold")

    if last_generated_at is not None:
        elapsed = (now - _as_aware(last_generated_at)).total_seconds()
        if elapsed < COOLDOWN_SECONDS:
            return TriggerDecision(False, "cooldown")

    fire_reason = "+".join(reasons)
    if not acquire:
        return TriggerDecision(True, fire_reason)

    redis = redis or cache.get_redis()
    token = await cache.acquire_lock(redis, user_id, ttl_seconds=cache.LOCK_TTL_SECONDS)
    if token is None:
        return TriggerDecision(False, "locked")
    return TriggerDecision(True, fire_reason, lock_token=token)


# --------------------------------------------------------------------------------------------------
# The orchestrator: gate → L1 → L2 → full run
# --------------------------------------------------------------------------------------------------
async def maybe_regenerate(
    user_id: uuid.UUID | str,
    *,
    high_intent: bool = False,
    session_factory=AsyncSessionLocal,
    vector_store: VectorStore | None = None,
    redis=None,
    run_agent_fn=run_agent,
    now: datetime | None = None,
) -> RegenerationOutcome:
    """Run the cost gate for ``user_id`` and, if it fires, serve from L1/L2 or do a full agent run.

    Called from the event-ingest background task (after persist + profile rebuild) and the feedback
    follow-up — never from a request handler's hot path. Always releases the Redis lock in a finally.
    ``run_agent_fn`` is injectable for tests; production uses ``app.agent.run_agent``.
    """
    uid = user_id if isinstance(user_id, uuid.UUID) else uuid.UUID(str(user_id))
    now = now or _utcnow()
    redis = redis or cache.get_redis()
    run_id = set_run_id()

    profile = await _read_profile(session_factory, uid)
    if profile is None:
        logger.info(
            "trigger.skip",
            extra={"extra_fields": {"user_id": str(uid), "reason": "no_profile", "run_id": run_id}},
        )
        return RegenerationOutcome(False, "no_profile", run_id=run_id)

    events_since_gen, profile_hash, interest_vector, last_generated_at, top_categories = profile

    # Drift *since the last generation* (not since the last rebuild): compare against the stashed
    # generation-time vector. No stash yet (never generated) → 0.0, so the first run is driven by the
    # events/high-intent arms and always lands on the full path below.
    gen_vector = await cache.load_generation_vector(redis, uid)
    drift = cosine_distance(interest_vector, gen_vector) if gen_vector else 0.0

    decision = await should_regenerate(
        uid,
        events_since_gen=events_since_gen,
        drift=drift,
        high_intent=high_intent,
        last_generated_at=last_generated_at,
        redis=redis,
        now=now,
    )
    if not decision.should_run:
        logger.info(
            "trigger.skip",
            extra={
                "extra_fields": {
                    "user_id": str(uid),
                    "reason": decision.reason,  # each early exit is a saved LLM call
                    "events_since_gen": events_since_gen,
                    "drift": round(drift, 4),
                    "high_intent": high_intent,
                    "run_id": run_id,
                }
            },
        )
        return RegenerationOutcome(False, decision.reason, run_id=run_id)

    try:
        gen_hash = await cache.load_generation_hash(redis, uid)

        # ---- L1: exact profile-hash match → serve the stored recommendation, no LLM ----
        if profile_hash and gen_hash and profile_hash == gen_hash:
            current = await _load_current_recommendation(session_factory, uid)
            if current is not None:
                await _write_cache_run(
                    session_factory,
                    uid,
                    run_id=run_id,
                    reason=decision.reason,
                    cache_hit="l1",
                    node_path=["l1_cache"],
                )
                # We handled the trigger (nothing materially changed) — clear the counter/cooldown so
                # we don't re-check on the next event. Baselines are unchanged, so no re-stash.
                await _mark_generated(
                    session_factory, redis, uid, now, events_since_gen, restash=False
                )
                logger.info(
                    "trigger.l1_hit",
                    extra={"extra_fields": {"user_id": str(uid), "run_id": run_id}},
                )
                return RegenerationOutcome(
                    True, decision.reason, cache_hit="l1",
                    recommendation_id=str(current["id"]), run_id=run_id,
                )

        # ---- L2: semantic near-match → reuse grounded items, re-personalize the narrative (1 call) ----
        if gen_vector and drift < L2_COSINE_DISTANCE_MAX:
            current = await _load_current_recommendation(session_factory, uid)
            if current is not None and current["items"]:
                rec_id = await _l2_repersonalize(
                    session_factory, uid, current, top_categories, decision.reason, run_id
                )
                await _mark_generated(
                    session_factory, redis, uid, now, events_since_gen, restash=True
                )
                logger.info(
                    "trigger.l2_hit",
                    extra={
                        "extra_fields": {
                            "user_id": str(uid),
                            "cosine_distance": round(drift, 4),
                            "run_id": run_id,
                        }
                    },
                )
                return RegenerationOutcome(
                    True, decision.reason, cache_hit="l2",
                    recommendation_id=rec_id, run_id=run_id,
                )

        # ---- miss: full seven-node agent run (L3 embedding cache inside Mesh still applies) ----
        recommendation = await run_agent_fn(
            uid,
            session_factory=session_factory,
            vector_store=vector_store,
            trigger_reason=decision.reason,
        )
        await _mark_generated(session_factory, redis, uid, now, events_since_gen, restash=True)
        logger.info(
            "trigger.full_run",
            extra={
                "extra_fields": {
                    "user_id": str(uid),
                    "reason": decision.reason,
                    "status": getattr(recommendation, "status", None),
                    "run_id": getattr(recommendation, "run_id", run_id),
                }
            },
        )
        return RegenerationOutcome(
            True,
            decision.reason,
            cache_hit=None,
            ran_full_agent=True,
            recommendation_id=getattr(recommendation, "recommendation_id", None),
            run_id=getattr(recommendation, "run_id", run_id),
        )
    finally:
        if decision.lock_token:
            await cache.release_lock(redis, uid, decision.lock_token)


# --------------------------------------------------------------------------------------------------
# The full-run path shared with POST /refresh (acquires the lock in the route, so no lock here)
# --------------------------------------------------------------------------------------------------
async def run_full_and_mark(
    user_id: uuid.UUID | str,
    *,
    trigger_reason: str = "manual",
    session_factory=AsyncSessionLocal,
    vector_store: VectorStore | None = None,
    redis=None,
    run_agent_fn=run_agent,
    now: datetime | None = None,
):
    """Do a full agent run and reset the trigger state (counter/cooldown/stash). Used by ``/refresh``,
    which already holds the ``gen:{user_id}`` lock, so this deliberately does not touch the lock."""
    uid = user_id if isinstance(user_id, uuid.UUID) else uuid.UUID(str(user_id))
    now = now or _utcnow()
    redis = redis or cache.get_redis()

    observed = await _read_events_since_gen(session_factory, uid)
    recommendation = await run_agent_fn(
        uid,
        session_factory=session_factory,
        vector_store=vector_store,
        trigger_reason=trigger_reason,
    )
    await _mark_generated(session_factory, redis, uid, now, observed, restash=True)
    return recommendation


# --------------------------------------------------------------------------------------------------
# L2 narrative re-personalization — the ONE cheap LLM call this module makes
# --------------------------------------------------------------------------------------------------
async def _l2_repersonalize(
    session_factory,
    uid: uuid.UUID,
    current: dict,
    top_categories: dict,
    reason: str,
    run_id: str,
) -> str:
    """Reuse the current recommendation's (already-grounded) items and rewrite only the narrative with
    a single cheap Mesh call, then persist a new current recommendation + its ``agent_runs`` row."""
    items = current["items"]
    narrative, usage = await _cheap_narrative(items, top_categories)
    headline = current.get("headline") or "Picks refreshed for you"

    async with session_factory() as session:
        agent_run = AgentRun(
            run_id=run_id,
            user_id=uid,
            trigger_reason=reason,
            node_path=["l2_cache", "generate"],
            cache_hit="l2",
            models_used={usage.model: {"calls": 1, "cost_usd": usage.cost_usd, "nodes": ["generate"]}},
            cost_usd=usage.cost_usd,
            latency_ms=int(usage.latency_ms),
            status="success",
            completed_at=_utcnow(),
        )
        session.add(agent_run)
        await session.flush()

        # Exactly one current recommendation per user (the WHERE is_current partial unique index).
        await session.execute(
            update(RecommendationRow)
            .where(RecommendationRow.user_id == uid, RecommendationRow.is_current.is_(True))
            .values(is_current=False)
        )
        rec_row = RecommendationRow(
            user_id=uid,
            agent_run_id=agent_run.id,
            headline=headline,
            narrative=narrative,
            items=items,  # reused, already grounded by validate_grounding on the prior full run
            trigger_reason=reason,
            is_current=True,
        )
        session.add(rec_row)
        await session.commit()
        await session.refresh(rec_row)
        return str(rec_row.id)


async def _cheap_narrative(items: list[dict], top_categories: dict):
    """One cheap-tier ``mesh.complete_text`` call that rewrites the narrative around the reused items.

    Node ``generate`` (so ``MESH_DISABLED`` returns the offline fixture narrative), but forced to the
    cheap tier — this is a small re-word, not a fresh generation.
    """
    titles = [str(item.get("title") or item.get("product_id")) for item in items]
    interests = ", ".join(list(top_categories or {})[:3]) or "your recent activity"
    system = (
        "You re-personalize a course recommendation blurb. The course list is FIXED — do not add, "
        "remove, rename, or reorder courses, and never invent prices or claims. Write 2-3 warm, "
        "specific sentences tying the picks to the learner's interests."
    )
    user = (
        f"Learner's strongest interests: {interests}.\n"
        f"Recommended courses (fixed): {', '.join(titles)}.\n"
        "Write the refreshed narrative only."
    )
    result = await mesh.complete_text(
        "generate",
        [{"role": "system", "content": system}, {"role": "user", "content": user}],
        tier="cheap",
        temperature=0.6,
        max_tokens=200,
    )
    narrative = (result.text or "").strip()
    if not narrative:
        narrative = "Based on what you have been exploring, these picks build on your recent interests."
    return narrative, result.usage


# --------------------------------------------------------------------------------------------------
# Small DB helpers
# --------------------------------------------------------------------------------------------------
async def _read_profile(session_factory, uid: uuid.UUID):
    """(events_since_gen, profile_hash, interest_vector, last_generated_at, top_categories) or None."""
    async with session_factory() as session:
        row = await session.get(UserProfile, uid)
        if row is None:
            return None
        return (
            int(row.events_since_gen or 0),
            row.profile_hash,
            list(row.interest_vector or []),
            row.last_generated_at,
            dict(row.top_categories or {}),
        )


async def _read_events_since_gen(session_factory, uid: uuid.UUID) -> int:
    async with session_factory() as session:
        value = await session.scalar(
            select(UserProfile.events_since_gen).where(UserProfile.user_id == uid)
        )
        return int(value or 0)


async def _load_current_recommendation(session_factory, uid: uuid.UUID) -> dict | None:
    """The user's single ``is_current`` recommendation as a plain dict, or ``None``."""
    async with session_factory() as session:
        row = await session.scalar(
            select(RecommendationRow).where(
                RecommendationRow.user_id == uid, RecommendationRow.is_current.is_(True)
            )
        )
        if row is None:
            return None
        return {
            "id": row.id,
            "headline": row.headline,
            "narrative": row.narrative,
            "items": list(row.items or []),
            "trigger_reason": row.trigger_reason,
        }


async def _write_cache_run(
    session_factory,
    uid: uuid.UUID,
    *,
    run_id: str,
    reason: str,
    cache_hit: str,
    node_path: list[str],
) -> str:
    """Persist an ``agent_runs`` telemetry row for a cache hit (no LLM, so cost/latency are ~0)."""
    async with session_factory() as session:
        agent_run = AgentRun(
            run_id=run_id,
            user_id=uid,
            trigger_reason=reason,
            node_path=node_path,
            cache_hit=cache_hit,
            models_used={},
            cost_usd=0.0,
            latency_ms=0,
            status="success",
            completed_at=_utcnow(),
        )
        session.add(agent_run)
        await session.commit()
        await session.refresh(agent_run)
        return str(agent_run.id)


async def _mark_generated(
    session_factory,
    redis,
    uid: uuid.UUID,
    now: datetime,
    observed_events: int,
    *,
    restash: bool,
) -> None:
    """Reset the trigger state after handling a trigger: clear the events counter (atomically, so a
    concurrent rebuild's increment is preserved), stamp ``last_generated_at``, and refresh the L1/L2
    baselines. Phase 5 explicitly left ``events_since_gen`` / ``last_generated_at`` for Phase 7 to own.
    """
    async with session_factory() as session:
        row = await session.get(UserProfile, uid)
        if row is None:
            return
        interest_vector = list(row.interest_vector or [])
        profile_hash = row.profile_hash or ""
        # Subtract only what we observed at decision time, floored at 0 — never clobber events a
        # concurrent (unlocked) profile rebuild appended while we were generating.
        await session.execute(
            update(UserProfile)
            .where(UserProfile.user_id == uid)
            .values(
                events_since_gen=func.greatest(
                    0, UserProfile.events_since_gen - observed_events
                ),
                last_generated_at=now,
            )
        )
        await session.commit()

    if restash:
        await cache.stash_generation_state(
            redis, uid, interest_vector=interest_vector, profile_hash=profile_hash
        )
