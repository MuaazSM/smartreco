"""Auth invariant tests (PRD §6.1, §8; IMPLEMENTATION.md Phase 3).

Hits the live app (and, through it, live Postgres) with the real `TestClient`. Self-contained: every
test mints its own unique `@example.com` email via `uuid4`, so re-runs never collide with the
`users.email` unique constraint and tests never depend on each other's rows.

The `client` fixture below is used as `with TestClient(app) as c:` deliberately, not bare
`TestClient(app)`. Starlette's `TestClient` only keeps a persistent anyio "blocking portal" (its
background-thread event loop) alive across requests while used as a context manager; without `with`,
every single request gets its own throwaway loop. The app's async SQLAlchemy engine
(`app.db.session.engine`) is a process-wide connection pool whose pooled asyncpg connections are
bound to whichever loop first used them — reusing one across two different throwaway loops raises
`RuntimeError: ... attached to a different loop` on the pool's pre-ping check (confirmed while writing
this test: it failed on every DB-touching request after the first when the client wasn't used as a
context manager). One module-scoped context manager keeps every request in this file on the same
loop. The one direct-SQL write below (promoting a user to admin — there is deliberately no API path
to do this) opens its own throwaway `asyncpg` connection rather than going through the shared engine,
for the same reason.

All HTTP calls share that one client, so tests that need to act as different users call
`client.cookies.clear()` between "sessions" rather than instantiating a second client.
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Iterator

import asyncpg
import pytest
from fastapi.testclient import TestClient

from app.core.config import settings
from app.core.security import decode_access_token
from app.main import app

_PASSWORD = "correct-horse-battery-staple"


@pytest.fixture(scope="module")
def client() -> Iterator[TestClient]:
    with TestClient(app) as c:
        yield c


def _unique_email(prefix: str = "authtest") -> str:
    return f"{prefix}-{uuid.uuid4().hex}@example.com"


def _register(client: TestClient, email: str, password: str = _PASSWORD) -> dict:
    response = client.post(
        "/api/auth/register",
        json={"email": email, "password": password, "display_name": "Test User"},
    )
    assert response.status_code == 201, response.text
    return response.json()


def _login(client: TestClient, email: str, password: str = _PASSWORD) -> None:
    response = client.post("/api/auth/login", json={"email": email, "password": password})
    assert response.status_code == 200, response.text


async def _promote_to_admin(user_id: str) -> None:
    dsn = settings.database_url.replace("postgresql+asyncpg://", "postgresql://", 1)
    conn = await asyncpg.connect(dsn)
    try:
        await conn.execute("UPDATE users SET role = 'admin' WHERE id = $1", uuid.UUID(user_id))
    finally:
        await conn.close()


def test_register_login_me_round_trip(client: TestClient) -> None:
    client.cookies.clear()
    email = _unique_email()

    registered = _register(client, email)
    assert registered["email"] == email
    assert registered["role"] == "user"
    assert "id" in registered

    _login(client, email)
    assert client.cookies.get("access_token") is not None

    me_response = client.get("/api/auth/me")
    assert me_response.status_code == 200
    me_body = me_response.json()
    assert me_body["email"] == email
    assert me_body["display_name"] == "Test User"
    assert me_body["role"] == "user"


def test_login_rejects_wrong_password(client: TestClient) -> None:
    client.cookies.clear()
    email = _unique_email()
    _register(client, email)

    response = client.post("/api/auth/login", json={"email": email, "password": "not-the-password"})
    assert response.status_code == 401


def test_me_without_cookie_is_unauthenticated(client: TestClient) -> None:
    client.cookies.clear()
    response = client.get("/api/auth/me")
    assert response.status_code == 401


def test_login_sets_httponly_samesite_cookie(client: TestClient) -> None:
    client.cookies.clear()
    email = _unique_email()
    _register(client, email)

    response = client.post("/api/auth/login", json={"email": email, "password": _PASSWORD})
    set_cookie = response.headers.get("set-cookie", "")
    assert "access_token=" in set_cookie
    assert "httponly" in set_cookie.lower()
    assert f"samesite={settings.cookie_samesite.lower()}" in set_cookie.lower()


def test_password_never_returned(client: TestClient) -> None:
    client.cookies.clear()
    email = _unique_email()

    registered = _register(client, email)
    assert "password" not in registered
    assert "password_hash" not in registered

    login_response = client.post("/api/auth/login", json={"email": email, "password": _PASSWORD})
    login_body = login_response.json()
    assert "password" not in login_body
    assert "password_hash" not in login_body

    me_body = client.get("/api/auth/me").json()
    assert "password" not in me_body
    assert "password_hash" not in me_body


def test_token_carries_role_claim(client: TestClient) -> None:
    client.cookies.clear()
    email = _unique_email()
    _register(client, email)
    _login(client, email)

    token = client.cookies.get("access_token")
    assert token is not None

    claims = decode_access_token(token)
    assert claims["role"] == "user"
    assert "sub" in claims
    assert "exp" in claims


def test_admin_guard_rejects_non_admin_and_allows_admin(client: TestClient) -> None:
    # An anonymous caller (no cookie at all) is 401, not 403 — auth failure precedes role check.
    client.cookies.clear()
    anon_response = client.get("/api/auth/_admin_probe")
    assert anon_response.status_code == 401

    # A freshly registered user defaults to role "user" and must be rejected by the guard.
    client.cookies.clear()
    non_admin_email = _unique_email("nonadmin")
    _register(client, non_admin_email)
    _login(client, non_admin_email)

    forbidden = client.get("/api/auth/_admin_probe")
    assert forbidden.status_code == 403

    # Promote a second user to admin directly in the DB (no self-registration path to admin exists,
    # nor should there be) and confirm the guard now lets them through.
    client.cookies.clear()
    admin_email = _unique_email("admin")
    admin_user = _register(client, admin_email)
    asyncio.run(_promote_to_admin(admin_user["id"]))

    _login(client, admin_email)
    allowed = client.get("/api/auth/_admin_probe")
    assert allowed.status_code == 200
    assert allowed.json()["role"] == "admin"
