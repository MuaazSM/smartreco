"""Trigger-policy + three-layer-cache invariant tests (PRD §6.5; CLAUDE.md invariant #6). Keep strong.

These prove the *cost gate* — the explicitly-judged claim that SmartReco does not call the LLM on every
event. They run against **live** Postgres + Redis (the lock and the L1/L2 baselines are real Redis
state) with ``MESH_DISABLED=true`` so the only "LLM" is the offline fixture — no network, deterministic.

What is pinned here:

  * the compound gate arithmetic (``should_regenerate``): threshold / cooldown / firing arms;
  * a below-threshold batch does **not** reach the agent (``run_agent`` never called);
  * an **L1** exact-hash hit serves the stored recommendation with zero LLM calls (``cache_hit='l1'``);
  * an **L2** semantic near-match reuses the grounded items and re-personalizes with **exactly one**
    cheap Mesh call, never the full graph (``cache_hit='l2'``);
  * the Redis lock **serializes** two concurrent triggers for one user — exactly one runs, the other
    early-exits on ``locked``.

Loop/pool discipline mirrors ``tests/test_dual_write.py``: every DB touch goes through a throwaway
``NullPool`` engine (passed as ``session_factory``) so nothing reuses the app engine's pool across the
per-test event loops; the Redis client and all trigger keys are created and cleaned per test.
"""

from __future__ import annotations

import os

# Offline + no background scheduler, set before any app import (see module docstring).
os.environ.setdefault("MESH_DISABLED", "true")
os.environ.setdefault("SMARTRECO_RUN_SCHEDULER", "false")

import asyncio  # noqa: E402
import types  # noqa: E402
import uuid  # noqa: E402
from datetime import datetime, timedelta, timezone  # noqa: E402

import pytest  # noqa: E402
import pytest_asyncio  # noqa: E402
import redis.asyncio as aioredis  # noqa: E402
from sqlalchemy import select  # noqa: E402
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402
from sqlalchemy.pool import NullPool  # noqa: E402

from app.core.config import settings  # noqa: E402
from app.db.models import AgentRun, User, UserProfile  # noqa: E402
from app.db.models import Recommendation as RecommendationRow  # noqa: E402
from app.llm import mesh  # noqa: E402
from app.services import cache, trigger  # noqa: E402


# --------------------------------------------------------------------------------------------------
# Fixtures — a throwaway NullPool engine + a dedicated Redis client, both cleaned per test
# --------------------------------------------------------------------------------------------------
@pytest_asyncio.fixture
async def factory():
    engine = create_async_engine(settings.database_url, poolclass=NullPool)
    session_factory = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
    try:
        yield session_factory
    finally:
        await engine.dispose()


@pytest_asyncio.fixture
async def redis_client():
    client = aioredis.from_url(settings.redis_url, encoding="utf-8", decode_responses=True)
    try:
        yield client
    finally:
        await client.aclose()


# --------------------------------------------------------------------------------------------------
# Seed helpers
# --------------------------------------------------------------------------------------------------
async def _seed_user(factory) -> uuid.UUID:
    async with factory() as session:
        user = User(
            email=f"trigger-{uuid.uuid4().hex}@example.com",
            password_hash="x",
            display_name="Trigger Test",
        )
        session.add(user)
        await session.commit()
        await session.refresh(user)
        return user.id


async def _seed_profile(
    factory,
    uid: uuid.UUID,
    *,
    events_since_gen: int,
    profile_hash: str | None,
    interest_vector: list[float],
    last_generated_at: datetime | None,
) -> None:
    async with factory() as session:
        session.add(
            UserProfile(
                user_id=uid,
                interest_vector=interest_vector,
                top_categories={"AI & LLMs": 5.0, "Machine Learning": 2.0},
                top_terms={"rag": 4.0},
                events_since_gen=events_since_gen,
                profile_hash=profile_hash,
                last_generated_at=last_generated_at,
            )
        )
        await session.commit()


async def _seed_current_rec(factory, uid: uuid.UUID) -> uuid.UUID:
    items = [
        {"product_id": str(uuid.uuid4()), "reason": "reused-a", "title": "Advanced RAG",
         "category": "AI & LLMs", "level": "advanced", "price_cents": 12900},
        {"product_id": str(uuid.uuid4()), "reason": "reused-b", "title": "Vector DBs",
         "category": "AI & LLMs", "level": "intermediate", "price_cents": 7900},
        {"product_id": str(uuid.uuid4()), "reason": "reused-c", "title": "LangGraph Agents",
         "category": "AI & LLMs", "level": "advanced", "price_cents": 13900},
    ]
    async with factory() as session:
        rec = RecommendationRow(
            user_id=uid,
            headline="Your stored picks",
            narrative="An older narrative that L2 should replace.",
            items=items,
            trigger_reason="seed",
            is_current=True,
        )
        session.add(rec)
        await session.commit()
        await session.refresh(rec)
        return rec.id


async def _read_profile_counter(factory, uid: uuid.UUID) -> int:
    async with factory() as session:
        return int(await session.scalar(
            select(UserProfile.events_since_gen).where(UserProfile.user_id == uid)
        ))


async def _latest_agent_run(factory, uid: uuid.UUID) -> AgentRun | None:
    async with factory() as session:
        return await session.scalar(
            select(AgentRun).where(AgentRun.user_id == uid).order_by(AgentRun.created_at.desc())
        )


async def _current_rec(factory, uid: uuid.UUID) -> RecommendationRow | None:
    async with factory() as session:
        return await session.scalar(
            select(RecommendationRow).where(
                RecommendationRow.user_id == uid, RecommendationRow.is_current.is_(True)
            )
        )


async def _cleanup(factory, redis_client, uid: uuid.UUID) -> None:
    await cache.clear_generation_state(redis_client, uid)
    async with factory() as session:
        user = await session.get(User, uid)
        if user is not None:
            await session.delete(user)  # cascades to profile / recs / events / agent_runs
            await session.commit()


class _RunAgentSpy:
    """Stand-in for ``run_agent`` that counts calls (and optionally sleeps to overlap concurrent runs)."""

    def __init__(self, *, sleep: float = 0.0) -> None:
        self.calls = 0
        self._sleep = sleep

    async def __call__(self, uid, *, session_factory=None, vector_store=None, trigger_reason="manual"):
        self.calls += 1
        if self._sleep:
            await asyncio.sleep(self._sleep)
        return types.SimpleNamespace(
            recommendation_id="fake-rec", run_id="fake-run", status="success"
        )


# --------------------------------------------------------------------------------------------------
# should_regenerate — the compound gate arithmetic (pure; no DB, no lock)
# --------------------------------------------------------------------------------------------------
async def test_gate_below_threshold_does_not_fire() -> None:
    d = await trigger.should_regenerate(
        uuid.uuid4(), events_since_gen=3, drift=0.0, high_intent=False,
        last_generated_at=None, acquire=False,
    )
    assert d.should_run is False and d.reason == "below_threshold"


async def test_gate_each_arm_fires() -> None:
    now = datetime.now(timezone.utc)
    old = now - timedelta(minutes=20)
    events = await trigger.should_regenerate(
        uuid.uuid4(), events_since_gen=8, drift=0.0, high_intent=False,
        last_generated_at=old, acquire=False,
    )
    drift = await trigger.should_regenerate(
        uuid.uuid4(), events_since_gen=0, drift=0.2, high_intent=False,
        last_generated_at=old, acquire=False,
    )
    intent = await trigger.should_regenerate(
        uuid.uuid4(), events_since_gen=0, drift=0.0, high_intent=True,
        last_generated_at=old, acquire=False,
    )
    assert events.should_run and "events" in events.reason
    assert drift.should_run and "drift" in drift.reason
    assert intent.should_run and "high_intent" in intent.reason


async def test_gate_cooldown_blocks_recent_generation() -> None:
    now = datetime.now(timezone.utc)
    d = await trigger.should_regenerate(
        uuid.uuid4(), events_since_gen=20, drift=0.9, high_intent=True,
        last_generated_at=now - timedelta(minutes=2), acquire=False, now=now,
    )
    assert d.should_run is False and d.reason == "cooldown"


def test_batch_has_high_intent() -> None:
    view = types.SimpleNamespace(event_type="view")
    cart = types.SimpleNamespace(event_type="cart")
    assert trigger.batch_has_high_intent([view]) is False
    assert trigger.batch_has_high_intent([view, cart]) is True


# --------------------------------------------------------------------------------------------------
# maybe_regenerate — below threshold, L1, L2, and the concurrency lock
# --------------------------------------------------------------------------------------------------
async def test_below_threshold_batch_never_reaches_the_agent(factory, redis_client) -> None:
    uid = await _seed_user(factory)
    try:
        await _seed_profile(
            factory, uid, events_since_gen=3, profile_hash="h",
            interest_vector=[1.0, 0.0, 0.0], last_generated_at=None,
        )
        spy = _RunAgentSpy()
        outcome = await trigger.maybe_regenerate(
            uid, high_intent=False, session_factory=factory, redis=redis_client, run_agent_fn=spy,
        )
        assert outcome.triggered is False
        assert outcome.reason == "below_threshold"
        assert spy.calls == 0  # the whole point: no LLM run for a small batch
    finally:
        await _cleanup(factory, redis_client, uid)


async def test_l1_exact_cache_serves_stored_rec_without_llm(factory, redis_client) -> None:
    uid = await _seed_user(factory)
    try:
        await _seed_profile(
            factory, uid, events_since_gen=10, profile_hash="HASH_A",
            interest_vector=[1.0, 0.0, 0.0],
            last_generated_at=datetime.now(timezone.utc) - timedelta(minutes=20),
        )
        rec_id = await _seed_current_rec(factory, uid)
        # Generation-time baseline: SAME hash → L1 must hit.
        await cache.stash_generation_state(
            redis_client, uid, interest_vector=[1.0, 0.0, 0.0], profile_hash="HASH_A"
        )

        spy = _RunAgentSpy()
        outcome = await trigger.maybe_regenerate(
            uid, session_factory=factory, redis=redis_client, run_agent_fn=spy,
        )

        assert outcome.triggered is True
        assert outcome.cache_hit == "l1"
        assert outcome.ran_full_agent is False
        assert spy.calls == 0  # served from the stored rec, no LLM
        assert outcome.recommendation_id == str(rec_id)
        # The trigger was handled → counter cleared and telemetry recorded as an l1 hit.
        assert await _read_profile_counter(factory, uid) == 0
        run = await _latest_agent_run(factory, uid)
        assert run is not None and run.cache_hit == "l1"
    finally:
        await _cleanup(factory, redis_client, uid)


async def test_l2_semantic_cache_reuses_items_with_one_cheap_call(
    factory, redis_client, monkeypatch
) -> None:
    uid = await _seed_user(factory)
    try:
        await _seed_profile(
            factory, uid, events_since_gen=10, profile_hash="HASH_NEW",
            interest_vector=[1.0, 0.0, 0.0],
            last_generated_at=datetime.now(timezone.utc) - timedelta(minutes=20),
        )
        await _seed_current_rec(factory, uid)
        # Hash CHANGED (L1 misses) but the interest vector is essentially unchanged (cosine 1.0 > 0.95)
        # → L2 must hit.
        await cache.stash_generation_state(
            redis_client, uid, interest_vector=[1.0, 0.0, 0.0], profile_hash="HASH_OLD"
        )

        # Count the ONE cheap re-personalization call (delegates to the offline fixture).
        calls = {"n": 0}
        original = mesh.complete_text

        async def counting_complete_text(node, messages, **kwargs):
            calls["n"] += 1
            return await original(node, messages, **kwargs)

        monkeypatch.setattr(mesh, "complete_text", counting_complete_text)

        spy = _RunAgentSpy()
        outcome = await trigger.maybe_regenerate(
            uid, session_factory=factory, redis=redis_client, run_agent_fn=spy,
        )

        assert outcome.triggered is True
        assert outcome.cache_hit == "l2"
        assert outcome.ran_full_agent is False
        assert spy.calls == 0                 # no full graph
        assert calls["n"] == 1                # exactly one cheap narrative call
        run = await _latest_agent_run(factory, uid)
        assert run is not None and run.cache_hit == "l2"
        # Items are reused (grounded on the prior full run); the narrative is refreshed.
        rec = await _current_rec(factory, uid)
        assert rec is not None
        assert [i["product_id"] for i in rec.items] and len(rec.items) == 3
    finally:
        await _cleanup(factory, redis_client, uid)


async def test_concurrent_triggers_serialize_on_the_lock(factory, redis_client) -> None:
    uid = await _seed_user(factory)
    try:
        await _seed_profile(
            factory, uid, events_since_gen=10, profile_hash="HASH_Z",
            interest_vector=[1.0, 0.0, 0.0],
            last_generated_at=datetime.now(timezone.utc) - timedelta(minutes=20),
        )
        # No generation stash → L1/L2 both miss → the winner takes the full-run path.
        spy = _RunAgentSpy(sleep=0.4)  # hold the lock long enough for the loser to collide

        results = await asyncio.gather(
            trigger.maybe_regenerate(
                uid, session_factory=factory, redis=redis_client, run_agent_fn=spy
            ),
            trigger.maybe_regenerate(
                uid, session_factory=factory, redis=redis_client, run_agent_fn=spy
            ),
        )

        assert spy.calls == 1  # exactly one agent run despite two concurrent triggers
        ran = [r for r in results if r.ran_full_agent]
        locked = [r for r in results if not r.triggered and r.reason == "locked"]
        assert len(ran) == 1
        assert len(locked) == 1
    finally:
        await _cleanup(factory, redis_client, uid)
