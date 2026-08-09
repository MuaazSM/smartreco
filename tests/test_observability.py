"""Observability tests (PRD §6.8 / F8; IMPLEMENTATION.md Phase 9b).

Runs against **live** Postgres, hitting the real ``GET /api/admin/agent-runs`` and
``GET /api/admin/metrics``. Proves:

  1. Both routes are admin-only (403 for a plain user).
  2. ``agent-runs`` returns rows the way ``app/agent/graph.py`` writes them — node path, retrieval
     score, refine loops, cache layer, per-model tokens/cost, latency, status, and (nullable until a
     live LangSmith key exists) the trace deep link — filterable to one user and paginated.
  3. ``metrics`` derives sane, internally-consistent numbers from ``agent_runs`` + ``events``:
     ``full_runs + l1_hits + l2_hits == total_runs`` (the ``cache_hit`` check constraint only allows
     NULL/'l1'/'l2'), ``cache_hit_rate`` and cost are non-negative, and the endpoint reacts to rows
     this test seeds itself.

Seeds its own ``agent_runs`` rows directly (own throwaway engine/pool, mirroring
``tests/test_dual_write.py``'s ``_drain_all`` pattern) rather than running the real graph — this
module owns no LLM/agent code, and a concurrent agent is using the DB elsewhere, so every assertion
either filters to this test's own ``user_id``/``run_id``s or only asserts invariants that hold
regardless of what else is in the table (never an exact global count).

Loop/pool discipline mirrors ``tests/test_auth.py``: **one** module-scoped ``with TestClient(app) as
c:`` for every HTTP call in this file (a second persistent-loop ``TestClient`` sharing the same
process-wide async engine pool raises ``RuntimeError: ... attached to a different loop`` on the pool's
pre-ping check — confirmed while writing this test). Switching "who's logged in" is done with
``client.cookies.clear()`` between sessions, not a second client. Every other out-of-band DB write
(promoting a user to admin, seeding ``agent_runs`` rows) opens its own throwaway connection/engine
under its own ``asyncio.run`` rather than going through the shared engine, for the same reason.
"""

from __future__ import annotations

import os

# Must be set before app import so the lifespan never starts the 5s background drain (mirrors the
# other live-DB tests) — this module doesn't touch the outbox at all, but importing app.main still
# triggers the same lifespan wiring.
os.environ["SMARTRECO_RUN_SCHEDULER"] = "false"

import asyncio  # noqa: E402
import uuid  # noqa: E402
from typing import Any, Iterator  # noqa: E402

import asyncpg  # noqa: E402
import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402
from sqlalchemy.pool import NullPool  # noqa: E402

from app.core.config import settings  # noqa: E402
from app.db.models import AgentRun  # noqa: E402
from app.main import app  # noqa: E402

_PASSWORD = "correct-horse-battery-staple"


# --------------------------------------------------------------------------------------------------
# Fixtures + helpers
# --------------------------------------------------------------------------------------------------
@pytest.fixture(scope="module")
def client() -> Iterator[TestClient]:
    with TestClient(app) as c:
        yield c


def _dsn() -> str:
    return settings.database_url.replace("postgresql+asyncpg://", "postgresql://", 1)


def _register(client: TestClient, prefix: str) -> tuple[str, str]:
    """Register a fresh user; returns ``(user_id, email)``. Does not log in."""
    email = f"{prefix}-{uuid.uuid4().hex}@example.com"
    resp = client.post(
        "/api/auth/register",
        json={"email": email, "password": _PASSWORD, "display_name": prefix},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["id"], email


def _login(client: TestClient, email: str) -> None:
    resp = client.post("/api/auth/login", json={"email": email, "password": _PASSWORD})
    assert resp.status_code == 200, resp.text


async def _promote_to_admin(user_id: str) -> None:
    conn = await asyncpg.connect(_dsn())
    try:
        await conn.execute("UPDATE users SET role = 'admin' WHERE id = $1", uuid.UUID(user_id))
    finally:
        await conn.close()


def _login_as_new_admin(client: TestClient, prefix: str = "obs-admin") -> str:
    """Register a fresh user, promote to admin, log in as them. Returns the admin's user_id."""
    admin_id, admin_email = _register(client, prefix)
    asyncio.run(_promote_to_admin(admin_id))
    _login(client, admin_email)
    return admin_id


def _insert_agent_runs(user_id: str, specs: list[dict[str, Any]]) -> list[str]:
    """Insert ``agent_runs`` rows via a throwaway NullPool engine (own loop, own pool).

    Uses the ORM model directly (not raw asyncpg) so JSONB/array columns serialize the same way
    ``app/agent/graph.py`` writes them in production, with no hand-rolled asyncpg codec handling.
    Returns the generated ``run_id``s in insertion order.
    """

    async def _run() -> list[str]:
        engine = create_async_engine(settings.database_url, poolclass=NullPool)
        factory = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
        run_ids: list[str] = []
        try:
            async with factory() as session:
                for spec in specs:
                    run_id = uuid.uuid4().hex
                    run_ids.append(run_id)
                    session.add(
                        AgentRun(
                            run_id=run_id,
                            user_id=uuid.UUID(user_id),
                            trigger_reason=spec.get("trigger_reason", "event"),
                            node_path=spec["node_path"],
                            retrieval_score=spec.get("retrieval_score"),
                            refine_loops=spec.get("refine_loops", 0),
                            cache_hit=spec.get("cache_hit"),
                            models_used=spec.get("models_used", {}),
                            cost_usd=spec.get("cost_usd", 0),
                            latency_ms=spec.get("latency_ms", 100),
                            langsmith_url=spec.get("langsmith_url"),
                            status=spec.get("status", "success"),
                        )
                    )
                await session.commit()
            return run_ids
        finally:
            await engine.dispose()

    return asyncio.run(_run())


_FULL_RUN_NODE_PATH = [
    "build_profile",
    "plan_queries",
    "retrieve",
    "grade_retrieval",
    "generate",
    "validate_grounding",
]
_CACHE_NODE_PATH = ["build_profile", "plan_queries", "retrieve", "grade_retrieval"]


# --------------------------------------------------------------------------------------------------
# Admin guard
# --------------------------------------------------------------------------------------------------
def test_agent_runs_requires_admin(client: TestClient) -> None:
    client.cookies.clear()
    _, email = _register(client, "obs-guard-runs")
    _login(client, email)
    resp = client.get("/api/admin/agent-runs")
    assert resp.status_code == 403, resp.text
    client.cookies.clear()


def test_metrics_requires_admin(client: TestClient) -> None:
    client.cookies.clear()
    _, email = _register(client, "obs-guard-metrics")
    _login(client, email)
    resp = client.get("/api/admin/metrics")
    assert resp.status_code == 403, resp.text
    client.cookies.clear()


# --------------------------------------------------------------------------------------------------
# /api/admin/agent-runs
# --------------------------------------------------------------------------------------------------
def test_agent_runs_lists_seeded_rows_for_one_user(client: TestClient) -> None:
    client.cookies.clear()
    subject_id, _ = _register(client, "obs-subject")

    run_ids = _insert_agent_runs(
        subject_id,
        [
            {
                "node_path": _FULL_RUN_NODE_PATH,
                "retrieval_score": 0.82,
                "refine_loops": 0,
                "cache_hit": None,
                "models_used": {
                    "llama-3.1-8b-instant": {
                        "calls": 2,
                        "prompt_tokens": 500,
                        "completion_tokens": 120,
                        "cost_usd": 0.0021,
                        "nodes": ["plan_queries", "generate"],
                    }
                },
                "cost_usd": 0.0021,
                "latency_ms": 2450,
                "status": "success",
                "trigger_reason": "event",
            },
            {
                "node_path": _CACHE_NODE_PATH,
                "retrieval_score": 0.91,
                "refine_loops": 0,
                "cache_hit": "l1",
                "models_used": {},
                "cost_usd": 0,
                "latency_ms": 35,
                "status": "success",
                "trigger_reason": "event",
            },
            {
                "node_path": _CACHE_NODE_PATH,
                "retrieval_score": 0.74,
                "refine_loops": 1,
                "cache_hit": "l2",
                "models_used": {
                    "llama-3.1-8b-instant": {
                        "calls": 1,
                        "prompt_tokens": 40,
                        "completion_tokens": 8,
                        "cost_usd": 0.0002,
                        "nodes": ["plan_queries"],
                    }
                },
                "cost_usd": 0.0002,
                "latency_ms": 310,
                "status": "success",
                "trigger_reason": "scheduled",
            },
        ],
    )

    _login_as_new_admin(client, "obs-admin-runs")

    resp = client.get(f"/api/admin/agent-runs?user_id={subject_id}&limit=50")
    assert resp.status_code == 200, resp.text
    body = resp.json()

    assert body["count"] == len(body["runs"])
    assert body["limit"] == 50
    assert body["offset"] == 0
    for row in body["runs"]:
        assert row["user_id"] == subject_id  # the user_id filter actually filtered

    by_run_id = {row["run_id"]: row for row in body["runs"]}
    assert set(run_ids) <= by_run_id.keys()

    full = by_run_id[run_ids[0]]
    assert full["cache_hit"] is None
    assert full["status"] == "success"
    assert full["node_path"] == _FULL_RUN_NODE_PATH
    assert full["retrieval_score"] == pytest.approx(0.82)
    assert full["refine_loops"] == 0
    assert full["cost_usd"] == pytest.approx(0.0021)
    assert full["latency_ms"] == 2450
    assert full["langsmith_url"] is None  # no live LANGSMITH_API_KEY in this environment
    assert full["models_used"]["llama-3.1-8b-instant"]["prompt_tokens"] == 500

    l1 = by_run_id[run_ids[1]]
    assert l1["cache_hit"] == "l1"
    assert l1["cost_usd"] == pytest.approx(0.0)

    l2 = by_run_id[run_ids[2]]
    assert l2["cache_hit"] == "l2"
    assert l2["refine_loops"] == 1
    assert l2["trigger_reason"] == "scheduled"

    client.cookies.clear()


def test_agent_runs_pagination_shape(client: TestClient) -> None:
    client.cookies.clear()
    _login_as_new_admin(client, "obs-admin-page")

    resp = client.get("/api/admin/agent-runs?limit=5&offset=0")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["limit"] == 5
    assert body["offset"] == 0
    assert len(body["runs"]) <= 5
    assert body["count"] == len(body["runs"])
    # Most-recent-first: created_at should be non-increasing across the page.
    created = [row["created_at"] for row in body["runs"]]
    assert created == sorted(created, reverse=True)

    client.cookies.clear()


def test_agent_runs_invalid_limit_rejected(client: TestClient) -> None:
    client.cookies.clear()
    _login_as_new_admin(client, "obs-admin-badlimit")

    resp = client.get("/api/admin/agent-runs?limit=0")
    assert resp.status_code == 422, resp.text

    client.cookies.clear()


# --------------------------------------------------------------------------------------------------
# /api/admin/metrics
# --------------------------------------------------------------------------------------------------
def test_metrics_returns_sane_numbers(client: TestClient) -> None:
    client.cookies.clear()
    subject_id, _ = _register(client, "obs-metrics-subject")

    # One full run, one l1 hit, one l2 hit — self-sufficient regardless of what other tests/agents
    # have already written to agent_runs.
    _insert_agent_runs(
        subject_id,
        [
            {"node_path": _FULL_RUN_NODE_PATH, "cache_hit": None, "cost_usd": 0.0015},
            {"node_path": _CACHE_NODE_PATH, "cache_hit": "l1", "cost_usd": 0},
            {"node_path": _CACHE_NODE_PATH, "cache_hit": "l2", "cost_usd": 0.0001},
        ],
    )

    _login_as_new_admin(client, "obs-admin-metrics")

    resp = client.get("/api/admin/metrics")
    assert resp.status_code == 200, resp.text
    body = resp.json()

    for key in (
        "llm_calls_per_100_events",
        "cache_hit_rate",
        "cost_total_usd",
        "cost_median_usd",
        "total_events",
        "total_runs",
        "full_runs",
        "l1_hits",
        "l2_hits",
    ):
        assert key in body, body

    # Internal consistency: the cache_hit check constraint only allows NULL/'l1'/'l2', so every run
    # falls into exactly one bucket regardless of what else is in the table.
    assert body["full_runs"] + body["l1_hits"] + body["l2_hits"] == body["total_runs"]
    assert body["total_runs"] >= 3  # at least the three rows this test just seeded
    assert body["full_runs"] >= 1
    assert body["l1_hits"] >= 1
    assert body["l2_hits"] >= 1

    assert 0.0 <= body["cache_hit_rate"] <= 1.0
    assert body["llm_calls_per_100_events"] >= 0.0
    assert body["cost_total_usd"] >= 0.0
    assert body["cost_median_usd"] >= 0.0
    assert body["total_events"] >= 0

    client.cookies.clear()
