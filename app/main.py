"""SmartReco FastAPI application entrypoint.

Phase 0 scaffolding: app instance, CORS, structured logging, an empty `lifespan` hook (Phase 4 wires
the 5s outbox-drain APScheduler job in here; later phases add more startup jobs), and `GET /health`
reporting live reachability of Postgres, Qdrant, Redis and Mesh.

Route routers (`app/api/routes/*`) are mounted by the phases that build them (auth in Phase 3,
products/admin in Phase 4, events/recommendations in Phases 5-7).
"""

from __future__ import annotations

import asyncio
import os
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

import asyncpg
import httpx
import redis.asyncio as aioredis
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from qdrant_client import AsyncQdrantClient

from app.api.routes.admin import router as admin_router
from app.api.routes.auth import router as auth_router
from app.api.routes.products import router as products_router
from app.core.config import settings
from app.core.logging import configure_logging, get_logger, set_run_id
from app.db.qdrant_bootstrap import bootstrap_qdrant
from app.db.session import dispose_engine, engine
from app.llm import model_router
from app.services import outbox_worker
from app.vector.qdrant_client import QdrantVectorStore

configure_logging(settings.log_level)
logger = get_logger(__name__)

_HEALTH_CHECK_TIMEOUT_SECONDS = 2.0

# The 5s outbox drain (PRD §6.2, §13.3). Read straight from the env in the lifespan rather than
# adding a config field (this phase owns no app/core/config.py change); default on. Tests set it to
# "false" so the background timer never races their deterministic, direct drain_outbox_once() calls.
_OUTBOX_DRAIN_INTERVAL_SECONDS = 5


def _run_scheduler_enabled() -> bool:
    return os.getenv("SMARTRECO_RUN_SCHEDULER", "true").strip().lower() not in {"false", "0", "no"}


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

    # Prime Mesh's free-tier catalog so live runs prefer free models (no-op fixture under
    # MESH_DISABLED — makes zero network calls). Non-fatal: resolve() falls back to paid if unprimed.
    try:
        await model_router.refresh_free_models()
    except Exception as exc:  # noqa: BLE001
        logger.error(
            "mesh.free_models_refresh_failed",
            extra={"extra_fields": {"detail": f"{type(exc).__name__}: {exc}"}},
        )

    # The transactional-outbox drain (invariant #3): in-process, continuous, every 5s. Must never
    # depend on an idle-sleeping host (PRD §13.3) — kept here even in deployment. Gated by env so the
    # test suite can disable the background timer.
    scheduler: AsyncIOScheduler | None = None
    worker_store: QdrantVectorStore | None = None
    if _run_scheduler_enabled():
        worker_store = QdrantVectorStore()
        scheduler = AsyncIOScheduler()
        scheduler.add_job(
            outbox_worker.scheduled_drain,
            trigger="interval",
            seconds=_OUTBOX_DRAIN_INTERVAL_SECONDS,
            args=[worker_store],
            id="outbox_drain",
            max_instances=1,
            coalesce=True,
            replace_existing=True,
        )
        scheduler.start()
        logger.info(
            "outbox.scheduler_started",
            extra={"extra_fields": {"interval_seconds": _OUTBOX_DRAIN_INTERVAL_SECONDS}},
        )
    else:
        logger.info("outbox.scheduler_disabled", extra={"extra_fields": {"reason": "env flag"}})

    yield

    if scheduler is not None:
        scheduler.shutdown(wait=False)
    if worker_store is not None:
        await worker_store.close()
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

app.include_router(auth_router)
app.include_router(products_router)
app.include_router(admin_router)


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
