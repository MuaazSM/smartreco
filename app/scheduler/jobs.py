"""The two daily cron jobs (PRD §6.7 / F7 + §13.3 "the scheduler trap"): **09:00** digest for
opted-in, recently-active users, and **03:00** a read-only vector-drift audit.

``register_daily_jobs`` adds both to the **existing** ``AsyncIOScheduler`` instance
``app/main.py``'s lifespan already runs the continuous 5s outbox drain on (PRD §13.3 — that drain
must stay in-process and continuous regardless of deployment topology; the daily jobs share its
scheduler rather than spinning up a second one). Registration itself is gated by ``app/main.py`` on
``SMARTRECO_RUN_SCHEDULER`` so the test suite never has a background cron firing mid-run.

**Local vs. deployed.** These cron jobs are the *local/long-running-process* path. A host that sleeps
on idle (free-tier web services, most serverless platforms) will simply never reach 09:00 in-process
— that is the trap PRD §13.3 names. The deployed path is
``.github/workflows/digest.yml`` (a GitHub Actions scheduled workflow) curling the token-protected
``POST /api/internal/run-digest`` (``app/api/routes/internal.py``), which calls
``run_daily_digest_job`` directly, bypassing this module's cron trigger entirely. Both paths call the
exact same function, so "which mode is this deployment using" is answered by whether
``SMARTRECO_RUN_SCHEDULER`` is left on for a long-lived host (rare in practice) or the Actions
workflow is the one firing it (the documented default — see the workflow file's header comment).

**Postgres jobstore.** PRD §6.7 asks for jobs to "survive restart" via a Postgres jobstore.
APScheduler's ``SQLAlchemyJobStore`` needs a *synchronous* DBAPI driver; this codebase is asyncpg-only
end to end (``app/db/session.py``, ``app/db/migrations/env.py``) and adding a sync driver
(``psycopg2-binary``) means editing ``requirements.txt``, which is outside this phase's file
ownership. ``_build_postgres_jobstore`` attempts it anyway (so it activates automatically if a sync
driver is ever added) and falls back to the default in-memory jobstore, logging clearly, if none is
importable — the same "never crash on infra absence, log and degrade" pattern
``app/main.py``'s lifespan already uses for Qdrant bootstrap and the free-model refresh. This has no
bearing on the deployed digest, which — per the scheduler trap above — never depends on this
in-process cron surviving a restart in the first place.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy import select

from app.core.config import settings
from app.core.logging import get_logger, set_run_id
from app.db.models import Event, User
from app.db.session import AsyncSessionLocal
from app.scheduler import digest
from app.services import cache
from app.services.catalog import active_product_ids
from app.vector.qdrant_client import QdrantVectorStore

logger = get_logger(__name__)

DAILY_DIGEST_JOB_ID = "daily_digest"
DRIFT_AUDIT_JOB_ID = "drift_audit"
_JOBSTORE_ALIAS = "postgres"

# UTC, not host-local time — deterministic regardless of where this process happens to run.
DIGEST_HOUR_UTC = 9
DRIFT_AUDIT_HOUR_UTC = 3


# --------------------------------------------------------------------------------------------------
# 09:00 — the digest job
# --------------------------------------------------------------------------------------------------
async def opted_in_active_user_ids(
    session_factory=AsyncSessionLocal, *, now: datetime | None = None
) -> list[User]:
    """Users with ``digest_optin`` set who logged at least one event in the last 24h (PRD §6.7:
    "for each opted-in user with activity in the last 24 hours")."""
    now = now or datetime.now(timezone.utc)
    since = now - timedelta(hours=24)
    async with session_factory() as session:
        active_ids = select(Event.user_id).where(Event.server_ts >= since).distinct().subquery()
        stmt = select(User).where(User.digest_optin.is_(True), User.id.in_(select(active_ids.c.user_id)))
        rows = (await session.execute(stmt)).scalars().all()
        return list(rows)


async def run_daily_digest_job(
    session_factory=AsyncSessionLocal, *, now: datetime | None = None
) -> dict:
    """Send (or, with no SMTP/Resend configured, render-and-save) the digest to every opted-in,
    recently-active user. One bad user must never abort the batch — each is wrapped individually.
    Called by both the local 09:00 cron (below) and ``POST /api/internal/run-digest``."""
    run_id = set_run_id()
    now = now or datetime.now(timezone.utc)
    redis_client = cache.get_redis()

    users = await opted_in_active_user_ids(session_factory, now=now)
    sent = 0
    rendered = 0
    skipped = 0
    failed = 0
    for user in users:
        try:
            result = await digest.send_digest_for_user(
                user, session_factory=session_factory, redis=redis_client, now=now
            )
        except Exception as exc:  # noqa: BLE001 - one user's failure must not sink the whole run
            failed += 1
            logger.error(
                "digest.user_failed",
                extra={
                    "extra_fields": {
                        "user_id": str(user.id),
                        "run_id": run_id,
                        "detail": f"{type(exc).__name__}: {exc}",
                    }
                },
            )
            continue
        if result.skipped:
            skipped += 1
            continue
        rendered += 1
        if result.sent:
            sent += 1

    summary = {
        "run_id": run_id,
        "candidates": len(users),
        "rendered": rendered,
        "sent": sent,
        "skipped": skipped,
        "failed": failed,
    }
    logger.info("digest.job_complete", extra={"extra_fields": summary})
    return summary


# --------------------------------------------------------------------------------------------------
# 03:00 — the vector-drift audit
# --------------------------------------------------------------------------------------------------
async def run_drift_audit_job(session_factory=AsyncSessionLocal) -> dict:
    """Read-only reconciliation of active Postgres products against Qdrant points — the same
    diff ``GET /api/admin/sync-status`` (``app/api/routes/admin.py``) performs, run here on a
    schedule so drift is caught even when nobody is looking at the admin console. "Alerting the
    admin console" is a structured ERROR-level log line (the same channel ``sync_status`` already
    uses for a live drift finding) plus a best-effort Telegram ping when that optional channel is
    configured; there is no separate alerts table in the current schema (``app/db/models.py`` is
    outside this phase's ownership) for this job to write to."""
    run_id = set_run_id()
    async with session_factory() as session:
        active_ids = await active_product_ids(session)

    store = QdrantVectorStore()
    try:
        point_ids = await store.all_point_ids()
    finally:
        await store.close()

    missing_in_vector = sorted(active_ids - point_ids)
    orphaned_in_vector = sorted(point_ids - active_ids)
    drift = bool(missing_in_vector) or bool(orphaned_in_vector)

    payload = {
        "run_id": run_id,
        "in_sync": not drift,
        "missing_in_vector": len(missing_in_vector),
        "orphaned_in_vector": len(orphaned_in_vector),
    }
    if drift:
        logger.error("scheduler.drift_audit_alert", extra={"extra_fields": payload})
        await digest.send_telegram_notification(
            f"[SmartReco] Vector drift detected: {len(missing_in_vector)} missing, "
            f"{len(orphaned_in_vector)} orphaned in Qdrant. Check /api/admin/sync-status."
        )
    else:
        logger.info("scheduler.drift_audit_ok", extra={"extra_fields": payload})
    return payload


# --------------------------------------------------------------------------------------------------
# Registration onto the existing scheduler
# --------------------------------------------------------------------------------------------------
def _build_postgres_jobstore():
    """Best-effort Postgres jobstore; ``None`` (default in-memory jobstore) if no sync DBAPI driver
    is importable. See module docstring for why that is expected in this repo today."""
    try:
        from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
    except ImportError:
        return None

    # SQLAlchemyJobStore drives a plain (sync) sqlalchemy.create_engine — strip the async driver
    # suffix so, IF a sync driver happens to be installed, the dialect resolves correctly.
    sync_url = settings.database_url.replace("postgresql+asyncpg://", "postgresql://", 1)
    try:
        store = SQLAlchemyJobStore(url=sync_url, tablename="apscheduler_jobs")
        return store
    except Exception as exc:  # noqa: BLE001 - a jobstore probe must never crash startup
        logger.warning(
            "scheduler.postgres_jobstore_unavailable",
            extra={"extra_fields": {"detail": f"{type(exc).__name__}: {exc}"}},
        )
        return None


def register_daily_jobs(scheduler: AsyncIOScheduler) -> None:
    """Add the 09:00 digest + 03:00 drift-audit cron jobs to ``scheduler`` (the same instance
    ``app/main.py``'s lifespan already runs the 5s outbox drain on). Call before ``scheduler.start()``.
    """
    jobstore = _build_postgres_jobstore()
    job_kwargs: dict = {}
    if jobstore is not None:
        scheduler.add_jobstore(jobstore, alias=_JOBSTORE_ALIAS)
        job_kwargs["jobstore"] = _JOBSTORE_ALIAS
        logger.info("scheduler.postgres_jobstore_active")
    else:
        logger.info("scheduler.postgres_jobstore_fallback_memory")

    scheduler.add_job(
        run_daily_digest_job,
        trigger="cron",
        hour=DIGEST_HOUR_UTC,
        minute=0,
        timezone=ZoneInfo("UTC"),
        id=DAILY_DIGEST_JOB_ID,
        max_instances=1,
        coalesce=True,
        replace_existing=True,
        **job_kwargs,
    )
    scheduler.add_job(
        run_drift_audit_job,
        trigger="cron",
        hour=DRIFT_AUDIT_HOUR_UTC,
        minute=0,
        timezone=ZoneInfo("UTC"),
        id=DRIFT_AUDIT_JOB_ID,
        max_instances=1,
        coalesce=True,
        replace_existing=True,
        **job_kwargs,
    )
    logger.info(
        "scheduler.daily_jobs_registered",
        extra={
            "extra_fields": {
                "digest_hour_utc": DIGEST_HOUR_UTC,
                "drift_audit_hour_utc": DRIFT_AUDIT_HOUR_UTC,
            }
        },
    )
