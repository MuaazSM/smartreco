"""Mesh `/models` catalog parsing + graceful-fallback tests for `app.llm.model_router`.

Regression coverage for the live bug the eval harness found: Mesh's `/v1/models` returns a **bare**
JSON list (`[{"id": ..., "is_free": ...}, ...]`), not the OpenAI SDK's expected paginated envelope
(`{"object": "list", "data": [...]}`). The SDK's typed `client.models.list()` chokes on the bare shape
deep in its pagination code, which crashed `refresh_free_models()` at startup and silently fell back to
paid models (which then 402 on a balance-less account) — quietly breaking the "free-tier per-node
routing through Mesh" claim.

These tests mock at the client boundary (`app.llm.mesh.get_client`), never touching a real network, and
assert:

  1. a bare-list payload resolves the correct free ids;
  2. a paginated `{"data": [...]}` payload resolves the same;
  3. ANY fetch/parse failure is swallowed into an empty free set, logged, and `resolve()` falls back to
     `PAID_FALLBACK` rather than crashing;
  4. `MESH_DISABLED=true` returns the fixture catalog with zero network calls (unchanged behavior).
"""

from __future__ import annotations

import httpx
import pytest

from app.core.config import settings
from app.llm import mesh, model_router


class _FakeMeshClient:
    """Stands in for `mesh.get_client()`'s return value — only `.get(...)` is exercised here."""

    def __init__(self, response: httpx.Response | None = None, exc: Exception | None = None) -> None:
        self._response = response
        self._exc = exc
        self.calls: list[tuple[str, dict]] = []

    async def get(self, path: str, *, cast_to: object = None) -> httpx.Response:
        self.calls.append((path, {"cast_to": cast_to}))
        if self._exc is not None:
            raise self._exc
        assert self._response is not None
        return self._response


@pytest.fixture(autouse=True)
def _reset_router_state(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every test starts from a clean, unprimed router and restores it afterward."""
    monkeypatch.setattr(model_router, "_live_free_models", frozenset())
    model_router.free_model_ids.cache_clear()
    yield
    monkeypatch.setattr(model_router, "_live_free_models", frozenset())
    model_router.free_model_ids.cache_clear()


async def test_bare_list_payload_resolves_free_ids(monkeypatch: pytest.MonkeyPatch) -> None:
    """Mesh's real shape: a bare JSON list, not a paginated envelope."""
    monkeypatch.setattr(settings, "mesh_disabled", False)
    payload = [
        {"id": "meta-llama/llama-3.3-70b-instruct", "is_free": True},
        {"id": "openai/gpt-4o-mini", "is_free": False},
    ]
    fake_client = _FakeMeshClient(response=httpx.Response(200, json=payload))
    monkeypatch.setattr(mesh, "get_client", lambda: fake_client)

    result = await model_router.refresh_free_models()

    assert result == frozenset({"meta-llama/llama-3.3-70b-instruct"})
    assert model_router.free_model_ids() == result
    # Confirms the low-level path was used (raw httpx.Response), not the SDK's typed models.list().
    assert fake_client.calls == [("/models", {"cast_to": httpx.Response})]


async def test_paginated_envelope_payload_resolves_free_ids(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tolerate the OpenAI-SDK-shaped envelope too, in case Mesh ever changes shape."""
    monkeypatch.setattr(settings, "mesh_disabled", False)
    payload = {
        "object": "list",
        "data": [
            {"id": "qwen/qwen-2.5-72b-instruct", "is_free": True},
            {"id": "deepseek/deepseek-chat", "is_free": True},
            {"id": "anthropic/claude-sonnet-4.5", "is_free": False},
        ],
    }
    fake_client = _FakeMeshClient(response=httpx.Response(200, json=payload))
    monkeypatch.setattr(mesh, "get_client", lambda: fake_client)

    result = await model_router.refresh_free_models()

    assert result == frozenset({"qwen/qwen-2.5-72b-instruct", "deepseek/deepseek-chat"})
    assert model_router.free_model_ids() == result


def _fake_request() -> httpx.Request:
    return httpx.Request("GET", "https://api.meshapi.ai/v1/models")


@pytest.mark.parametrize(
    "fake_client",
    [
        # A network-level failure while sending the request (timeout, connection reset, ...).
        _FakeMeshClient(exc=httpx.ConnectTimeout("timed out")),
        # What the real SDK raises when `response.raise_for_status()` sees a 402 (balance-less
        # account) — the SDK checks status *before* our code ever sees the body, so this is what
        # `_fetch_model_catalog` actually has to survive in production, not a 402-shaped JSON body.
        _FakeMeshClient(
            exc=httpx.HTTPStatusError(
                "402 Payment Required", request=_fake_request(), response=httpx.Response(402)
            )
        ),
        # A payload that is neither a bare list nor a `{"data": [...]}` envelope — parsed successfully
        # by `.json()` but rejected by our own shape check, exercising the explicit `TypeError` raise.
        _FakeMeshClient(response=httpx.Response(200, json="not-a-list-or-dict")),
    ],
    ids=["network-timeout", "402-payment-required", "unexpected-shape"],
)
async def test_any_failure_falls_back_to_empty_set_and_paid_model(
    monkeypatch: pytest.MonkeyPatch, fake_client: _FakeMeshClient
) -> None:
    """Startup must never crash because Mesh's catalog endpoint is down/unaffordable/reshaped."""
    monkeypatch.setattr(settings, "mesh_disabled", False)
    monkeypatch.setattr(mesh, "get_client", lambda: fake_client)

    result = await model_router.refresh_free_models()

    assert result == frozenset()
    assert model_router.free_model_ids() == frozenset()
    # resolve() must cleanly fall back to PAID_FALLBACK, never raise or return a free id.
    assert model_router.resolve("generate") == model_router.PAID_FALLBACK["quality"]
    assert model_router.resolve("plan_queries") == model_router.PAID_FALLBACK["cheap"]


async def test_mesh_disabled_returns_fixture_catalog_with_no_network(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unchanged offline behavior: MESH_DISABLED must never touch the network."""
    monkeypatch.setattr(settings, "mesh_disabled", True)

    def _explode() -> None:
        raise AssertionError("get_client() must not be called under MESH_DISABLED")

    monkeypatch.setattr(mesh, "get_client", _explode)

    result = await model_router.refresh_free_models()

    assert result == frozenset(model_router.FREE_PREFERENCE)
    assert model_router.free_model_ids() == frozenset(model_router.FREE_PREFERENCE)
    # resolve() should happily pick the top preference since the whole list is "free" under fixtures.
    assert model_router.resolve("generate") == model_router.FREE_PREFERENCE[0]
