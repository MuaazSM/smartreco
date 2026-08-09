"""Scheduled-digest invariant tests (PRD §6.7 / F7; IMPLEMENTATION.md Phase 9a).

Runs against **live** Postgres + Redis with ``MESH_DISABLED=true`` (mirrors ``tests/test_trigger.py``:
a throwaway ``NullPool`` engine per test, its own Redis client, no shared app-engine pool). The agent
itself is stubbed via the same ``run_agent_fn`` injection seam ``app.services.trigger`` already
exposes (``_RunAgentSpy``-style) — these tests pin the *digest* plumbing (hook text, HTML rendering,
delivery degradation, the token-protected trigger endpoint, unsubscribe), not the seven-node graph,
which ``tests/test_grounding.py`` already covers end to end.

What is pinned here:

  * ``digest.build_digest_for_user`` renders non-empty HTML carrying the headline/narrative/hook/
    unsubscribe link for an opted-in user with recent (last-24h) activity;
  * ``digest.send_digest_for_user`` degrades gracefully with no SMTP/Resend configured — content is
    saved to disk, never lost, and the function never raises;
  * ``POST /api/internal/run-digest`` 401s with a missing/wrong ``X-Digest-Token`` and 200s with the
    right one (settings.digest_trigger_token is unset in ``.env`` by design — CLAUDE.md invariant #4
    — so an unconfigured token must reject every call, not accept any);
  * ``GET /api/internal/unsubscribe`` flips ``users.digest_optin`` off only for a validly-signed link;
  * ``scripts/send_digest_now.py`` handles the "no such user" and "no candidates" paths without
    crashing and reports the shared graceful-degradation contract's outcome faithfully.
"""

from __future__ import annotations

import os

# Offline + no background scheduler, set before any app import (mirrors tests/test_trigger.py).
os.environ.setdefault("MESH_DISABLED", "true")
os.environ.setdefault("SMARTRECO_RUN_SCHEDULER", "false")

import importlib.util  # noqa: E402
import uuid  # noqa: E402
from datetime import datetime, timedelta, timezone  # noqa: E402
from pathlib import Path  # noqa: E402
from typing import Iterator  # noqa: E402
from urllib.parse import parse_qs, urlsplit  # noqa: E402

import pytest  # noqa: E402
import pytest_asyncio  # noqa: E402
import redis.asyncio as aioredis  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import select  # noqa: E402
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402
from sqlalchemy.pool import NullPool  # noqa: E402

from app.core.config import settings  # noqa: E402
from app.db.models import Event, Product, User, UserProfile  # noqa: E402
from app.main import app  # noqa: E402
from app.scheduler import digest  # noqa: E402

_REPO_ROOT = Path(__file__).resolve().parents[1]


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


@pytest.fixture(scope="module")
def client() -> Iterator[TestClient]:
    with TestClient(app) as c:
        yield c


# --------------------------------------------------------------------------------------------------
# Seed helpers
# --------------------------------------------------------------------------------------------------
async def _seed_user(factory, *, digest_optin: bool = True) -> tuple[uuid.UUID, str]:
    email = f"digest-{uuid.uuid4().hex}@example.com"
    async with factory() as session:
        user = User(
            email=email, password_hash="x", display_name="Digest Test", digest_optin=digest_optin
        )
        session.add(user)
        await session.commit()
        await session.refresh(user)
        return user.id, email


async def _seed_profile(factory, uid: uuid.UUID) -> None:
    async with factory() as session:
        session.add(
            UserProfile(
                user_id=uid,
                interest_vector=[1.0, 0.0, 0.0],
                top_categories={"Machine Learning": 5.0},
                top_terms={"rag": 4.0},
                events_since_gen=1,
                profile_hash="seed-hash",
                last_generated_at=None,
            )
        )
        await session.commit()


async def _seed_product(factory, *, category: str = "Machine Learning") -> uuid.UUID:
    async with factory() as session:
        product = Product(
            title="Advanced RAG for Digest Test",
            description="A course used only by tests/test_digest.py.",
            category=category,
            tags=["rag"],
            level="advanced",
            price_cents=12900,
            is_active=True,
        )
        session.add(product)
        await session.commit()
        await session.refresh(product)
        return product.id


async def _seed_recent_dwell_event(
    factory, uid: uuid.UUID, product_id: uuid.UUID, *, dwell_ms: float = 1_200_000.0
) -> None:
    """A 20-minute dwell in the last 24h — enough for the hook to name a duration (PRD §6.7's
    "Yesterday you spent 20 minutes in advanced RAG" example)."""
    now = datetime.now(timezone.utc)
    async with factory() as session:
        session.add(
            Event(
                user_id=uid,
                session_id=uuid.uuid4(),
                event_type="dwell",
                product_id=product_id,
                payload={"dwell_ms": dwell_ms},
                weight=2.5,
                client_ts=now - timedelta(hours=1),
            )
        )
        await session.commit()


async def _digest_optin(factory, uid: uuid.UUID) -> bool:
    async with factory() as session:
        return bool(await session.scalar(select(User.digest_optin).where(User.id == uid)))


async def _cleanup(factory, redis_client, uid: uuid.UUID, product_id: uuid.UUID | None = None) -> None:
    from app.services import cache

    await cache.clear_generation_state(redis_client, uid)
    async with factory() as session:
        user = await session.get(User, uid)
        if user is not None:
            await session.delete(user)  # cascades to profile / events / recommendations / agent_runs
            await session.commit()
        if product_id is not None:
            product = await session.get(Product, product_id)
            if product is not None:
                await session.delete(product)
                await session.commit()


class _StubRecommendation:
    """Minimal stand-in for ``app.agent.Recommendation`` — just the fields the digest reads."""

    def __init__(self, run_id: str) -> None:
        self.headline = "Your next step in Machine Learning"
        self.narrative = "You have been deep in retrieval-augmented generation this week."
        self.trigger_reason = "scheduled"
        self.run_id = run_id
        self.status = "success"
        self.recommendation_id = "fake-rec"
        self.items = [
            {
                "product_id": str(uuid.uuid4()),
                "reason": "Builds directly on the RAG course you dwelled on.",
                "title": "Vector Databases Deep Dive",
                "category": "Machine Learning",
                "level": "intermediate",
                "price_cents": 7900,
            }
        ]


class _StubRunAgent:
    """Stand-in for ``run_agent`` (mirrors ``tests/test_trigger.py::_RunAgentSpy``) — no LLM call, no
    live agent graph run; this file pins the digest plumbing, not the graph itself."""

    def __init__(self) -> None:
        self.calls = 0

    async def __call__(self, uid, *, session_factory=None, vector_store=None, trigger_reason="manual"):
        self.calls += 1
        return _StubRecommendation(run_id=f"stub-run-{self.calls}")


# --------------------------------------------------------------------------------------------------
# build_digest_for_user — renders non-empty HTML with the recap-with-hook
# --------------------------------------------------------------------------------------------------
async def test_build_digest_for_user_renders_nonempty_html_with_hook(factory, redis_client) -> None:
    uid, email = await _seed_user(factory)
    product_id = None
    try:
        await _seed_profile(factory, uid)
        product_id = await _seed_product(factory, category="Machine Learning")
        await _seed_recent_dwell_event(factory, uid, product_id, dwell_ms=1_200_000.0)

        user = User(id=uid, email=email, display_name="Digest Test")  # detached, just for rendering
        stub = _StubRunAgent()
        result = await digest.build_digest_for_user(
            user, session_factory=factory, redis=redis_client, run_agent_fn=stub
        )

        assert stub.calls == 1
        assert result.html.strip() != ""
        assert "Your next step in Machine Learning" in result.html
        assert "Vector Databases Deep Dive" in result.html
        assert "Machine Learning" in result.hook
        assert "20 minutes" in result.hook
        assert "/api/internal/unsubscribe" in result.html
        assert str(uid) in result.html
    finally:
        await _cleanup(factory, redis_client, uid, product_id)


# --------------------------------------------------------------------------------------------------
# send_digest_for_user — graceful degradation with no SMTP/Resend configured
# --------------------------------------------------------------------------------------------------
async def test_send_digest_for_user_degrades_gracefully_without_smtp(
    factory, redis_client, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(settings, "smtp_host", None)
    monkeypatch.setattr(settings, "resend_api_key", None)

    uid, email = await _seed_user(factory)
    product_id = None
    try:
        await _seed_profile(factory, uid)
        product_id = await _seed_product(factory)
        await _seed_recent_dwell_event(factory, uid, product_id)

        user = User(id=uid, email=email, display_name="Digest Test")
        stub = _StubRunAgent()
        result = await digest.send_digest_for_user(
            user,
            session_factory=factory,
            redis=redis_client,
            run_agent_fn=stub,
            save_dir=tmp_path,
        )

        assert result.skipped is False
        assert result.sent is False
        assert result.channel is None
        assert result.saved_path is not None
        saved = Path(result.saved_path)
        assert saved.exists()
        assert saved.read_text(encoding="utf-8").strip() != ""
        assert "Your next step in Machine Learning" in saved.read_text(encoding="utf-8")
    finally:
        await _cleanup(factory, redis_client, uid, product_id)


# --------------------------------------------------------------------------------------------------
# POST /api/internal/run-digest — token gate
# --------------------------------------------------------------------------------------------------
def test_run_digest_rejects_missing_or_unconfigured_token(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Unconfigured (the real .env.example default): every call rejected, even a plausible-looking one.
    monkeypatch.setattr(settings, "digest_trigger_token", None)
    response = client.post("/api/internal/run-digest", headers={"X-Digest-Token": "anything"})
    assert response.status_code == 401

    response = client.post("/api/internal/run-digest")
    assert response.status_code == 401


def test_run_digest_rejects_wrong_token_accepts_right_token(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "digest_trigger_token", "correct-horse-battery-staple")

    wrong = client.post("/api/internal/run-digest", headers={"X-Digest-Token": "nope"})
    assert wrong.status_code == 401

    async def _fake_job(*args, **kwargs) -> dict:
        return {"run_id": "x", "candidates": 0, "rendered": 0, "sent": 0, "skipped": 0, "failed": 0}

    import app.api.routes.internal as internal_module

    monkeypatch.setattr(internal_module.scheduler_jobs, "run_daily_digest_job", _fake_job)

    right = client.post(
        "/api/internal/run-digest",
        headers={"X-Digest-Token": "correct-horse-battery-staple"},
    )
    assert right.status_code == 200, right.text
    body = right.json()
    assert body == {"run_id": "x", "candidates": 0, "rendered": 0, "sent": 0, "skipped": 0, "failed": 0}


# --------------------------------------------------------------------------------------------------
# GET /api/internal/unsubscribe — signed link flips digest_optin
# --------------------------------------------------------------------------------------------------
async def test_unsubscribe_requires_valid_token_then_flips_optin(client: TestClient, factory) -> None:
    uid, _ = await _seed_user(factory, digest_optin=True)
    try:
        url = digest.unsubscribe_url(uid)
        parsed = urlsplit(url)
        real_token = parse_qs(parsed.query)["token"][0]

        wrong = client.get(f"/api/internal/unsubscribe?user_id={uid}&token=not-the-real-token")
        assert wrong.status_code == 403
        assert await _digest_optin(factory, uid) is True

        right = client.get(f"/api/internal/unsubscribe?user_id={uid}&token={real_token}")
        assert right.status_code == 200
        assert "unsubscribed" in right.text.lower()
        assert await _digest_optin(factory, uid) is False
    finally:
        async with factory() as session:
            user = await session.get(User, uid)
            if user is not None:
                await session.delete(user)
                await session.commit()


# --------------------------------------------------------------------------------------------------
# scripts/send_digest_now.py — real script code paths, no live agent run needed
# --------------------------------------------------------------------------------------------------
def _load_send_digest_now_module():
    path = _REPO_ROOT / "scripts" / "send_digest_now.py"
    spec = importlib.util.spec_from_file_location("send_digest_now_under_test", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def test_send_digest_now_reports_missing_user_without_crashing(
    factory, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    module = _load_send_digest_now_module()
    # Swap the script's module-level `AsyncSessionLocal` (bound to the shared, process-wide app
    # engine) for this test's own throwaway NullPool factory — mirrors why tests/test_auth.py never
    # touches the shared engine outside a single persistent TestClient loop: reusing a pooled asyncpg
    # connection across two different event loops (this bare async test's loop vs. some other test's
    # TestClient portal loop) raises "attached to a different loop".
    monkeypatch.setattr(module, "AsyncSessionLocal", factory)
    exit_code = await module._run(f"no-such-user-{uuid.uuid4().hex}@example.com", None)
    assert exit_code == 1
    assert "No user found" in capsys.readouterr().err


async def test_send_digest_now_reports_no_candidates_without_crashing(
    monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    module = _load_send_digest_now_module()

    async def _no_candidates(session_factory, *, now=None):
        return []

    monkeypatch.setattr(module.jobs, "opted_in_active_user_ids", _no_candidates)

    exit_code = await module._run(None, None)
    assert exit_code == 0
    assert "No opted-in users" in capsys.readouterr().out


async def test_send_digest_now_prints_saved_path_when_not_sent(
    monkeypatch: pytest.MonkeyPatch, capsys, tmp_path: Path
) -> None:
    module = _load_send_digest_now_module()
    fake_user = User(id=uuid.uuid4(), email="stub@example.com", display_name="Stub")

    async def _one_candidate(session_factory, *, now=None):
        return [fake_user]

    async def _fake_send(user, *, save_dir=None, **kwargs):
        return digest.DigestResult(
            user_id=str(user.id),
            email=user.email,
            headline="Stub headline",
            html="<html>stub</html>",
            hook="stub hook",
            run_id="stub-run",
            sent=False,
            channel=None,
            saved_path=str(tmp_path / "stub.html"),
        )

    monkeypatch.setattr(module.jobs, "opted_in_active_user_ids", _one_candidate)
    monkeypatch.setattr(module.digest, "send_digest_for_user", _fake_send)

    exit_code = await module._run(None, tmp_path)
    out = capsys.readouterr().out
    assert exit_code == 0
    assert "not sent" in out
    assert str(tmp_path / "stub.html") in out
    assert "0/1 delivered" in out
