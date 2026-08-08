"""SmartReco FastAPI application entrypoint.

Phase 0 scaffolding: app instance, CORS, structured logging, an empty `lifespan` hook (Phase 4 wires
the 5s outbox-drain APScheduler job in here; later phases add more startup jobs), and `GET /health`
reporting live reachability of Postgres, Qdrant, Redis and Mesh.

Route routers (`app/api/routes/*`) are mounted by the phases that build them (auth in Phase 3,
products/admin in Phase 4, events/recommendations in Phases 5-7).
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

import asyncpg
import httpx
import redis.asyncio as aioredis
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from qdrant_client import AsyncQdrantClient

from app.core.config import settings
from app.core.logging import configure_logging, get_logger, set_run_id
from app.db.qdrant_bootstrap import bootstrap_qdrant
from app.db.session import dispose_engine, engine

configure_logging(settings.log_level)
logger = get_logger(__name__)

_HEALTH_CHECK_TIMEOUT_SECONDS = 2.0


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """App startup/shutdown hook.

    Phase 1 (this): the async DB engine is created at import time (`app.db.session.engine`,
    imported above so the module-level `create_async_engine` runs); nothing schema-related happens
    here — Alembic owns schema. Also runs the idempotent Qdrant `products` collection bootstrap so
    a fresh environment never demos against a missing collection. Qdrant unreachability at startup
    is logged, not fatal — `GET /health` already reports it, and failing app boot on a transient
    infra hiccup would be worse than starting degraded (same philosophy as the `_check_*` health
    helpers below, which never raise).

    Phase 4 starts the APScheduler outbox-drain loop here too (must be in-process and continuous);
    later phases add the digest/drift jobs. See PRD §13.3 — this is the scheduler that must never
    depend on an idle-sleeping host.
    """
    set_run_id()
    logger.info(
        "smartreco.startup",
        extra={
            "extra_fields": {
                "environment": settings.environment,
                "db_driver": engine.url.drivername,
            }
        },
    )
    try:
        await bootstrap_qdrant()
    except Exception as exc:  # noqa: BLE001 - startup must never crash the app on infra hiccups
        logger.error(
            "qdrant.bootstrap_failed",
            extra={"extra_fields": {"detail": f"{type(exc).__name__}: {exc}"}},
        )
    yield
    await dispose_engine()
    logger.info("smartreco.shutdown")


app = FastAPI(title=settings.app_name, lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


def _asyncpg_dsn(database_url: str) -> str:
    """asyncpg.connect() wants a plain postgres(ql):// DSN, not SQLAlchemy's `+asyncpg` suffix."""
    return database_url.replace("postgresql+asyncpg://", "postgresql://", 1)


async def _check_database() -> dict[str, Any]:
    try:
        conn = await asyncpg.connect(
            _asyncpg_dsn(settings.database_url), timeout=_HEALTH_CHECK_TIMEOUT_SECONDS
        )
        try:
            await conn.fetchval("SELECT 1")
        finally:
            await conn.close()
        return {"status": "ok"}
    except Exception as exc:  # noqa: BLE001 - health check must never raise
        return {"status": "error", "detail": f"{type(exc).__name__}: {exc}"}


async def _check_qdrant() -> dict[str, Any]:
    try:
        client = AsyncQdrantClient(
            url=settings.qdrant_url,
            api_key=settings.qdrant_api_key,
            timeout=_HEALTH_CHECK_TIMEOUT_SECONDS,
        )
        try:
            await client.get_collections()
        finally:
            await client.close()
        return {"status": "ok"}
    except Exception as exc:  # noqa: BLE001
        return {"status": "error", "detail": f"{type(exc).__name__}: {exc}"}


async def _check_redis() -> dict[str, Any]:
    client = aioredis.from_url(settings.redis_url, socket_timeout=_HEALTH_CHECK_TIMEOUT_SECONDS)
    try:
        await client.ping()
        return {"status": "ok"}
    except Exception as exc:  # noqa: BLE001
        return {"status": "error", "detail": f"{type(exc).__name__}: {exc}"}
    finally:
        await client.aclose()


async def _check_mesh() -> dict[str, Any]:
    if settings.mesh_disabled:
        return {"status": "disabled", "detail": "MESH_DISABLED=true — fixtures in use"}
    if not settings.mesh_api_key:
        return {"status": "not_configured", "detail": "MESH_API_KEY is not set"}
    try:
        async with httpx.AsyncClient(timeout=_HEALTH_CHECK_TIMEOUT_SECONDS) as client:
            response = await client.get(
                f"{settings.mesh_base_url}/models",
                headers={"Authorization": f"Bearer {settings.mesh_api_key}"},
            )
        if response.status_code < 500:
            # Any non-5xx (including 401 on a bad key) means the gateway itself is reachable.
            return {"status": "ok", "http_status": response.status_code}
        return {"status": "error", "detail": f"HTTP {response.status_code}"}
    except Exception as exc:  # noqa: BLE001
        return {"status": "error", "detail": f"{type(exc).__name__}: {exc}"}


@app.get("/health")
async def health() -> dict[str, Any]:
    """Reachability of every external dependency: db, qdrant, redis, mesh (PRD §8, §13.5 #1)."""
    db_result, qdrant_result, redis_result, mesh_result = await asyncio.gather(
        asyncio.wait_for(_check_database(), timeout=_HEALTH_CHECK_TIMEOUT_SECONDS + 1),
        asyncio.wait_for(_check_qdrant(), timeout=_HEALTH_CHECK_TIMEOUT_SECONDS + 1),
        asyncio.wait_for(_check_redis(), timeout=_HEALTH_CHECK_TIMEOUT_SECONDS + 1),
        asyncio.wait_for(_check_mesh(), timeout=_HEALTH_CHECK_TIMEOUT_SECONDS + 1),
        return_exceptions=True,
    )

    components = {}
    for name, result in (
        ("db", db_result),
        ("qdrant", qdrant_result),
        ("redis", redis_result),
        ("mesh", mesh_result),
    ):
        if isinstance(result, BaseException):
            components[name] = {"status": "error", "detail": f"{type(result).__name__}: {result}"}
        else:
            components[name] = result

    ok_statuses = {"ok", "disabled", "not_configured"}
    overall = "ok" if all(c["status"] in ok_statuses for c in components.values()) else "degraded"

    return {"status": overall, "components": components}
