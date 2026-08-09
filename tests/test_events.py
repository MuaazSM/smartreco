"""Event-ingest + profile end-to-end tests (PRD §6.3; IMPLEMENTATION.md Phase 5).

Runs against **live** Postgres + Qdrant, hitting the real ``POST /api/events/batch``. Proves the
Phase 5 acceptance criteria:

  1. A batch is **accepted with 202** and the events land with **server-assigned weights** — a
     client-supplied ``weight`` in the body is ignored.
  2. Re-sending the same batch **dedupes on the natural key** (retry inserts nothing).
  3. The profile is rebuilt: ``user_profiles`` gets a **non-empty decayed centroid**, a
     ``profile_hash``, and ``events_since_gen`` bumped by exactly the number of *new* events.

FastAPI ``BackgroundTasks`` run inside the response lifecycle, so Starlette's ``TestClient`` blocks
until the bulk insert + profile recompute finish — by the time ``client.post`` returns, the rows and
the profile row already exist, making these assertions deterministic without polling.

Loop/pool discipline mirrors ``tests/test_dual_write.py``: HTTP goes through one context-managed
``TestClient`` (one persistent portal loop for the app engine's pool), and every out-of-band DB read
runs under its own ``asyncio.run`` on a throwaway ``asyncpg`` connection.
"""

from __future__ import annotations

import os

# Disable the background outbox drain before importing the app (mirrors the other live tests).
os.environ["SMARTRECO_RUN_SCHEDULER"] = "false"

import asyncio  # noqa: E402
import uuid  # noqa: E402
from datetime import datetime, timedelta, timezone  # noqa: E402
from typing import Iterator  # noqa: E402

import asyncpg  # noqa: E402
import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.core.config import settings  # noqa: E402
from app.main import app  # noqa: E402

_PASSWORD = "correct-horse-battery-staple"


@pytest.fixture(scope="module")
def user_client() -> Iterator[TestClient]:
    """A TestClient logged in as a fresh (non-admin) user."""
    with TestClient(app) as c:
        email = f"events-user-{uuid.uuid4().hex}@example.com"
        registered = c.post(
            "/api/auth/register",
            json={"email": email, "password": _PASSWORD, "display_name": "Events User"},
        )
        assert registered.status_code == 201, registered.text
        user_id = registered.json()["id"]
        login = c.post("/api/auth/login", json={"email": email, "password": _PASSWORD})
        assert login.status_code == 200, login.text
        c.user_id = user_id  # type: ignore[attr-defined]
        yield c


def _dsn() -> str:
    return settings.database_url.replace("postgresql+asyncpg://", "postgresql://", 1)


def _synced_product_ids(limit: int = 2) -> list[str]:
    """Active products that are already vector-synced (so the centroid has real vectors to average)."""

    async def _run() -> list[str]:
        conn = await asyncpg.connect(_dsn())
        try:
            rows = await conn.fetch(
                "SELECT id FROM products "
                "WHERE is_active = true AND vector_synced_at IS NOT NULL "
                "ORDER BY created_at LIMIT $1",
                limit,
            )
            return [str(r["id"]) for r in rows]
        finally:
            await conn.close()

    return asyncio.run(_run())


def _events_for(user_id: str, session_id: str) -> list[asyncpg.Record]:
    async def _run() -> list[asyncpg.Record]:
        conn = await asyncpg.connect(_dsn())
        try:
            return await conn.fetch(
                "SELECT event_type, weight, product_id, payload FROM events "
                "WHERE user_id = $1 AND session_id = $2 ORDER BY client_ts",
                uuid.UUID(user_id),
                uuid.UUID(session_id),
            )
        finally:
            await conn.close()

    return asyncio.run(_run())


def _profile_row(user_id: str) -> asyncpg.Record | None:
    async def _run() -> asyncpg.Record | None:
        conn = await asyncpg.connect(_dsn())
        try:
            return await conn.fetchrow(
                "SELECT interest_vector, top_categories, top_terms, profile_hash, events_since_gen "
                "FROM user_profiles WHERE user_id = $1",
                uuid.UUID(user_id),
            )
        finally:
            await conn.close()

    return asyncio.run(_run())


def _batch_body(product_ids: list[str], session_id: str) -> dict:
    """A representative batch: search + view/click/dwell/cart, each with a distinct client_ts.

    Every event also carries a bogus ``weight`` to prove the server ignores it, and a client-generated
    ``event_id`` for idempotency traceability.
    """
    base = datetime.now(timezone.utc) - timedelta(minutes=30)
    pid0 = product_ids[0]
    pid1 = product_ids[1] if len(product_ids) > 1 else product_ids[0]

    def ev(i: int, event_type: str, **extra: object) -> dict:
        return {
            "session_id": session_id,
            "event_type": event_type,
            "client_ts": (base + timedelta(seconds=i)).isoformat(),
            "event_id": str(uuid.uuid4()),
            "weight": 999.0,  # client weight — MUST be ignored by the server
            **extra,
        }

    return {
        "events": [
            ev(0, "search", payload={"query": "deep learning pytorch"}),
            ev(1, "view", product_id=pid0),
            ev(2, "click", product_id=pid0),
            ev(3, "dwell", product_id=pid0, payload={"dwell_ms": 45000}),
            ev(4, "cart", product_id=pid1),
        ]
    }


_EXPECTED_WEIGHTS = {"search": 2.0, "view": 1.0, "click": 1.5, "dwell": 2.5, "cart": 3.0}


def test_batch_accepts_202_lands_with_server_weights_and_ignores_client_weight(
    user_client: TestClient,
) -> None:
    product_ids = _synced_product_ids(2)
    assert len(product_ids) >= 1, "seed_data must have created vector-synced products"
    session_id = str(uuid.uuid4())

    response = user_client.post("/api/events/batch", json=_batch_body(product_ids, session_id))
    assert response.status_code == 202, response.text
    assert response.json()["accepted"] == 5

    rows = _events_for(user_client.user_id, session_id)  # type: ignore[attr-defined]
    assert len(rows) == 5
    for row in rows:
        # Server assigned the weight from the event type — the client's 999.0 was discarded.
        assert row["weight"] == pytest.approx(_EXPECTED_WEIGHTS[row["event_type"]])
        assert row["weight"] != pytest.approx(999.0)


def test_retry_is_idempotent_on_natural_key(user_client: TestClient) -> None:
    product_ids = _synced_product_ids(2)
    session_id = str(uuid.uuid4())
    body = _batch_body(product_ids, session_id)

    first = user_client.post("/api/events/batch", json=body)
    assert first.status_code == 202
    count_after_first = len(_events_for(user_client.user_id, session_id))  # type: ignore[attr-defined]
    assert count_after_first == 5

    # Replay the identical batch (same natural keys) -> ON CONFLICT DO NOTHING -> no new rows.
    second = user_client.post("/api/events/batch", json=body)
    assert second.status_code == 202
    count_after_retry = len(_events_for(user_client.user_id, session_id))  # type: ignore[attr-defined]
    assert count_after_retry == 5


def test_profile_rebuilt_with_decayed_centroid_and_bumped_counter(user_client: TestClient) -> None:
    # Use a dedicated fresh user so events_since_gen is exactly the size of this batch.
    email = f"events-profile-{uuid.uuid4().hex}@example.com"
    reg = user_client.post(
        "/api/auth/register",
        json={"email": email, "password": _PASSWORD, "display_name": "Profile User"},
    )
    assert reg.status_code == 201, reg.text
    user_id = reg.json()["id"]
    user_client.cookies.clear()
    assert user_client.post("/api/auth/login", json={"email": email, "password": _PASSWORD}).status_code == 200

    product_ids = _synced_product_ids(2)
    session_id = str(uuid.uuid4())
    resp = user_client.post("/api/events/batch", json=_batch_body(product_ids, session_id))
    assert resp.status_code == 202, resp.text

    row = _profile_row(user_id)
    assert row is not None, "profile row should exist after ingest"
    # 4 of the 5 events reference vector-synced products -> a non-empty decayed centroid.
    assert len(row["interest_vector"]) > 0
    assert row["profile_hash"] is not None
    # events_since_gen == number of distinct events just ingested (5); only a generation run resets it.
    assert row["events_since_gen"] == 5
    # The search term fed top_terms; the product events fed top_categories.
    import json as _json

    top_terms = row["top_terms"] if isinstance(row["top_terms"], dict) else _json.loads(row["top_terms"])
    top_categories = (
        row["top_categories"]
        if isinstance(row["top_categories"], dict)
        else _json.loads(row["top_categories"])
    )
    assert "pytorch" in top_terms
    assert len(top_categories) >= 1
