"""Phase 0 smoke test: the app imports cleanly and `/health` is wired up.

Deliberately does not require live Postgres/Qdrant/Redis — CI runs this without docker services,
so it only asserts the endpoint responds with the expected shape, not that every component is "ok".
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.main import app


def test_app_imports_and_health_route_registered() -> None:
    route_paths = {route.path for route in app.routes}
    assert "/health" in route_paths


def test_health_endpoint_reports_all_four_components() -> None:
    client = TestClient(app)
    response = client.get("/health")

    assert response.status_code == 200
    body = response.json()

    assert "status" in body
    assert body["status"] in {"ok", "degraded"}

    assert set(body["components"].keys()) == {"db", "qdrant", "redis", "mesh"}
    for component in body["components"].values():
        assert "status" in component
