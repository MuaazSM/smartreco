"""Dual-write invariant tests (PRD §6.2 + Figure 3; CLAUDE.md invariant #3). Keep strong.

These prove the submission's core differentiator end to end against **live** Postgres + Qdrant:

  1. Creating a product writes the ``products`` row **and** a *pending* ``vector_outbox`` row in one
     transaction, and the request handler makes **no** Qdrant write (no point exists yet).
  2. Draining the outbox syncs Qdrant (point id = product id), marks the row ``done``, and stamps
     ``products.vector_synced_at``.
  3. ``GET /api/admin/sync-status`` then reports ``in_sync: true`` with nothing missing/orphaned.
  4. A no-op update recomputes the same ``content_hash``, enqueues **nothing**, and re-embeds nothing.
  5. A content change enqueues one row and re-embeds once; a delete soft-deletes + enqueues a
     ``delete`` row, and draining removes the point while sync-status stays ``in_sync: true``.

The background APScheduler drain is disabled here (``SMARTRECO_RUN_SCHEDULER=false``, set before the
app is imported) so nothing races these deterministic, direct ``drain_outbox_once`` calls — the test
asserts the *pending* state, then drains explicitly and asserts the *done* state.

Loop discipline mirrors ``tests/test_auth.py``: HTTP goes through one context-managed ``TestClient``
(one persistent portal loop for the shared async engine's pool); every out-of-band DB read, Qdrant
check, and outbox drain runs under its own ``asyncio.run`` against a *throwaway* ``NullPool`` engine /
fresh client, so nothing reuses the app engine's pool across loops.
"""

from __future__ import annotations

import os

# Must be set before app import so the lifespan never starts the 5s background drain (see module doc).
os.environ["SMARTRECO_RUN_SCHEDULER"] = "false"

import asyncio  # noqa: E402
import uuid  # noqa: E402
from typing import Iterator  # noqa: E402

import asyncpg  # noqa: E402
import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402
from sqlalchemy.pool import NullPool  # noqa: E402

from app.core.config import settings  # noqa: E402
from app.llm import mesh  # noqa: E402
from app.main import app  # noqa: E402
from app.services.outbox_worker import drain_outbox_once  # noqa: E402
from app.vector.qdrant_client import QdrantVectorStore  # noqa: E402

_PASSWORD = "correct-horse-battery-staple"


# --------------------------------------------------------------------------------------------------
# Fixtures + helpers
# --------------------------------------------------------------------------------------------------
@pytest.fixture(scope="module")
def admin_client() -> Iterator[TestClient]:
    """A TestClient logged in as a freshly-minted admin (promoted directly in the DB)."""
    with TestClient(app) as c:
        email = f"dualwrite-admin-{uuid.uuid4().hex}@example.com"
        registered = c.post(
            "/api/auth/register",
            json={"email": email, "password": _PASSWORD, "display_name": "Dual Write Admin"},
        )
        assert registered.status_code == 201, registered.text
        asyncio.run(_promote_to_admin(registered.json()["id"]))
        login = c.post("/api/auth/login", json={"email": email, "password": _PASSWORD})
        assert login.status_code == 200, login.text
        yield c


def _dsn() -> str:
    return settings.database_url.replace("postgresql+asyncpg://", "postgresql://", 1)


async def _promote_to_admin(user_id: str) -> None:
    conn = await asyncpg.connect(_dsn())
    try:
        await conn.execute("UPDATE users SET role = 'admin' WHERE id = $1", uuid.UUID(user_id))
    finally:
        await conn.close()


def _create_product(client: TestClient, title: str, **overrides) -> dict:
    body = {
        "title": title,
        "description": "A hands-on course for the dual-write test.",
        "category": "Machine Learning",
        "tags": ["ml", "test"],
        "level": "intermediate",
        "price_cents": 4900,
    }
    body.update(overrides)
    response = client.post("/api/admin/products", json=body)
    assert response.status_code == 201, response.text
    return response.json()


def _drain_all() -> int:
    """Drain every due outbox row, using a throwaway NullPool engine (own loop, own pool)."""

    async def _run() -> int:
        engine = create_async_engine(settings.database_url, poolclass=NullPool)
        factory = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
        store = QdrantVectorStore()
        total = 0
        try:
            while True:
                processed = await drain_outbox_once(
                    session_factory=factory, vector_store=store, limit=100
                )
                total += processed
                if processed == 0:
                    break
            return total
        finally:
            await store.close()
            await engine.dispose()

    return asyncio.run(_run())


def _qdrant_point_exists(product_id: str) -> bool:
    async def _run() -> bool:
        store = QdrantVectorStore()
        try:
            return await store.retrieve(product_id) is not None
        finally:
            await store.close()

    return asyncio.run(_run())


def _product_row(product_id: str) -> asyncpg.Record | None:
    async def _run() -> asyncpg.Record | None:
        conn = await asyncpg.connect(_dsn())
        try:
            return await conn.fetchrow(
                "SELECT content_hash, vector_synced_at, is_active FROM products WHERE id = $1",
                uuid.UUID(product_id),
            )
        finally:
            await conn.close()

    return asyncio.run(_run())


def _outbox_rows(product_id: str) -> list[asyncpg.Record]:
    async def _run() -> list[asyncpg.Record]:
        conn = await asyncpg.connect(_dsn())
        try:
            return await conn.fetch(
                "SELECT op, status FROM vector_outbox WHERE product_id = $1 ORDER BY id",
                uuid.UUID(product_id),
            )
        finally:
            await conn.close()

    return asyncio.run(_run())


def _pending_outbox_count(product_id: str) -> int:
    async def _run() -> int:
        conn = await asyncpg.connect(_dsn())
        try:
            return await conn.fetchval(
                "SELECT count(*) FROM vector_outbox WHERE product_id = $1 AND status = 'pending'",
                uuid.UUID(product_id),
            )
        finally:
            await conn.close()

    return asyncio.run(_run())


# --------------------------------------------------------------------------------------------------
# Tests
# --------------------------------------------------------------------------------------------------
def test_create_writes_product_and_pending_outbox_in_one_tx_without_qdrant(
    admin_client: TestClient,
) -> None:
    created = _create_product(admin_client, f"Atomic Create {uuid.uuid4().hex}")
    product_id = created["id"]

    # Postgres side: the product exists and carries a content_hash but is not yet vector-synced.
    row = _product_row(product_id)
    assert row is not None
    assert row["content_hash"] is not None
    assert row["vector_synced_at"] is None
    assert row["is_active"] is True

    # Exactly one outbox row, an upsert, still pending — committed in the same tx as the product.
    rows = _outbox_rows(product_id)
    assert len(rows) == 1
    assert rows[0]["op"] == "upsert"
    assert rows[0]["status"] == "pending"

    # The handler made NO Qdrant write: no point exists until the worker drains the outbox.
    assert _qdrant_point_exists(product_id) is False


def test_drain_syncs_qdrant_marks_done_and_stamps_synced_at(admin_client: TestClient) -> None:
    created = _create_product(admin_client, f"Drain Sync {uuid.uuid4().hex}")
    product_id = created["id"]
    assert _qdrant_point_exists(product_id) is False

    _drain_all()

    assert _qdrant_point_exists(product_id) is True
    rows = _outbox_rows(product_id)
    assert [r["status"] for r in rows] == ["done"]
    row = _product_row(product_id)
    assert row is not None
    assert row["vector_synced_at"] is not None


def test_sync_status_reports_in_sync(admin_client: TestClient) -> None:
    _create_product(admin_client, f"Sync Status {uuid.uuid4().hex}")
    _drain_all()  # flush all pending work so the whole system is caught up

    response = admin_client.get("/api/admin/sync-status")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["in_sync"] is True, body
    assert body["missing_in_vector"] == []
    assert body["orphaned_in_vector"] == []
    assert body["failed_count"] == 0


def test_unchanged_update_enqueues_nothing_and_skips_reembed(
    admin_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    title = f"Unchanged Update {uuid.uuid4().hex}"
    created = _create_product(admin_client, title)
    product_id = created["id"]
    _drain_all()
    hash_before = _product_row(product_id)["content_hash"]

    # Spy on the ONE embedding path (mesh.embed). embeddings.embed_product calls it module-qualified,
    # so patching the attribute here is what it will see.
    calls = {"n": 0}
    original_embed = mesh.embed

    async def _counting_embed(*args, **kwargs):
        calls["n"] += 1
        return await original_embed(*args, **kwargs)

    monkeypatch.setattr(mesh, "embed", _counting_embed)

    # PATCH with the SAME title -> identical embeddable content -> no content_hash change.
    response = admin_client.patch(f"/api/admin/products/{product_id}", json={"title": title})
    assert response.status_code == 200, response.text

    # No new outbox row was enqueued, so there is nothing to re-embed...
    assert _pending_outbox_count(product_id) == 0
    # ...and draining confirms it: mesh.embed is never called for an unchanged update.
    assert _drain_all() == 0
    assert calls["n"] == 0

    assert _product_row(product_id)["content_hash"] == hash_before


def test_content_change_enqueues_and_reembeds_once(
    admin_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    created = _create_product(admin_client, f"Content Change {uuid.uuid4().hex}")
    product_id = created["id"]
    _drain_all()  # sync the create and empty the global queue
    hash_before = _product_row(product_id)["content_hash"]

    calls = {"n": 0}
    original_embed = mesh.embed

    async def _counting_embed(*args, **kwargs):
        calls["n"] += 1
        return await original_embed(*args, **kwargs)

    monkeypatch.setattr(mesh, "embed", _counting_embed)

    # A real content change -> new content_hash -> exactly one new pending upsert row.
    response = admin_client.patch(
        f"/api/admin/products/{product_id}",
        json={"title": f"Content Change (revised) {uuid.uuid4().hex}"},
    )
    assert response.status_code == 200, response.text
    assert _product_row(product_id)["content_hash"] != hash_before
    assert _pending_outbox_count(product_id) == 1

    assert _drain_all() == 1
    assert calls["n"] == 1  # re-embedded exactly once for the changed text

    status = admin_client.get("/api/admin/sync-status").json()
    assert status["in_sync"] is True, status


def test_delete_removes_qdrant_point_and_stays_in_sync(admin_client: TestClient) -> None:
    created = _create_product(admin_client, f"Delete Me {uuid.uuid4().hex}")
    product_id = created["id"]
    _drain_all()
    assert _qdrant_point_exists(product_id) is True

    response = admin_client.delete(f"/api/admin/products/{product_id}")
    assert response.status_code == 200, response.text
    assert response.json()["is_active"] is False

    # Soft-delete + a pending delete outbox row; the handler made no Qdrant write.
    assert _product_row(product_id)["is_active"] is False
    ops = [r["op"] for r in _outbox_rows(product_id)]
    assert ops[-1] == "delete"
    assert _qdrant_point_exists(product_id) is True  # still present until the worker runs

    _drain_all()
    assert _qdrant_point_exists(product_id) is False  # worker removed the point

    status = admin_client.get("/api/admin/sync-status").json()
    assert status["in_sync"] is True, status
    assert product_id not in status["orphaned_in_vector"]
    assert product_id not in status["missing_in_vector"]


def test_deactivate_via_patch_removes_point_and_reactivate_restores_it(
    admin_client: TestClient,
) -> None:
    # Regression guard: deactivating via PATCH is_active:false must DELETE the Qdrant point (not
    # upsert an inactive one), or the point orphans forever and sync-status is permanently false.
    created = _create_product(admin_client, f"Toggle Active {uuid.uuid4().hex}")
    product_id = created["id"]
    _drain_all()
    assert _qdrant_point_exists(product_id) is True

    # PATCH is_active:false -> enqueues a delete op (mirrors DELETE /products/{id}).
    off = admin_client.patch(f"/api/admin/products/{product_id}", json={"is_active": False})
    assert off.status_code == 200, off.text
    assert off.json()["is_active"] is False
    assert [r["op"] for r in _outbox_rows(product_id)][-1] == "delete"

    _drain_all()
    assert _qdrant_point_exists(product_id) is False  # point removed, not left orphaned
    status_off = admin_client.get("/api/admin/sync-status").json()
    assert status_off["in_sync"] is True, status_off
    assert product_id not in status_off["orphaned_in_vector"]

    # PATCH is_active:true -> re-adds the point via an upsert.
    on = admin_client.patch(f"/api/admin/products/{product_id}", json={"is_active": True})
    assert on.status_code == 200, on.text
    assert on.json()["is_active"] is True
    assert [r["op"] for r in _outbox_rows(product_id)][-1] == "upsert"

    _drain_all()
    assert _qdrant_point_exists(product_id) is True  # point restored
    status_on = admin_client.get("/api/admin/sync-status").json()
    assert status_on["in_sync"] is True, status_on
    assert product_id not in status_on["missing_in_vector"]


def test_sync_status_not_in_sync_while_update_pending(admin_client: TestClient) -> None:
    # Honesty of the in_sync claim: a pending content-update (id in both stores, but Qdrant vector is
    # stale) must report in_sync:false until the drain settles it.
    created = _create_product(admin_client, f"Pending Honesty {uuid.uuid4().hex}")
    product_id = created["id"]
    _drain_all()
    assert admin_client.get("/api/admin/sync-status").json()["in_sync"] is True

    resp = admin_client.patch(
        f"/api/admin/products/{product_id}",
        json={"title": f"Pending Honesty (revised) {uuid.uuid4().hex}"},
    )
    assert resp.status_code == 200, resp.text

    pending = admin_client.get("/api/admin/sync-status").json()
    assert pending["pending_count"] >= 1
    assert pending["in_sync"] is False  # still settling — not truthfully in sync yet
    # ...neither missing nor orphaned catches it (the id is in both sets); pending_count is what does.
    assert product_id not in pending["missing_in_vector"]
    assert product_id not in pending["orphaned_in_vector"]

    _drain_all()
    assert admin_client.get("/api/admin/sync-status").json()["in_sync"] is True
