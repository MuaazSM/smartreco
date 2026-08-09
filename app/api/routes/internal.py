"""Token-protected internal endpoints — the deployed half of the "scheduler trap" (PRD §13.3).

A free/idle-sleeping host never fires ``app/scheduler/jobs.py``'s in-process 09:00 APScheduler cron
(that job store is only reliably alive on a long-running local/dev process). The deployed digest
instead fires via a GitHub Actions scheduled workflow (``.github/workflows/digest.yml``) curling
``POST /api/internal/run-digest`` with a shared secret in the ``X-Digest-Token`` header, compared
against ``settings.digest_trigger_token`` (env ``DIGEST_TRIGGER_TOKEN``). Both paths — the local cron
and this route — call the exact same ``app.scheduler.jobs.run_daily_digest_job``.

**Fail closed.** ``DIGEST_TRIGGER_TOKEN`` ships blank in ``.env.example`` (CLAUDE.md invariant #4:
no secrets committed). An unset token means every request is rejected with 401 — never "anybody may
trigger the digest because nobody configured a secret." The comparison uses
``hmac.compare_digest`` (constant-time) rather than ``==``.

Also hosts the digest's one-click unsubscribe link (``GET /unsubscribe``): a click from an email
client carries no session cookie, so it is authorized instead by an HMAC of the user id
(``app.scheduler.digest.verify_unsubscribe_token`` — keyed on the existing ``settings.jwt_secret``, no
new secret) and simply flips ``users.digest_optin`` off.
"""

from __future__ import annotations

import hmac
import uuid

from fastapi import APIRouter, Header, HTTPException, Query, status
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from sqlalchemy import update

from app.core.config import settings
from app.core.logging import get_logger
from app.db.models import User
from app.db.session import AsyncSessionLocal
from app.scheduler import jobs as scheduler_jobs
from app.scheduler.digest import verify_unsubscribe_token

logger = get_logger(__name__)

router = APIRouter(prefix="/api/internal", tags=["internal"])


class RunDigestOut(BaseModel):
    """Summary of one digest batch — the same shape ``app.scheduler.jobs.run_daily_digest_job``
    returns, so the GitHub Actions log and a local manual call see identical output."""

    run_id: str
    candidates: int
    rendered: int
    sent: int
    skipped: int
    failed: int


def _require_digest_token(x_digest_token: str | None) -> None:
    configured = settings.digest_trigger_token
    if not configured or not x_digest_token or not hmac.compare_digest(configured, x_digest_token):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing or invalid X-Digest-Token",
        )


@router.post("/run-digest", response_model=RunDigestOut)
async def run_digest(
    x_digest_token: str | None = Header(default=None, alias="X-Digest-Token"),
) -> RunDigestOut:
    """Fire the daily digest immediately. Token-protected (see module docstring); this is exactly
    what ``.github/workflows/digest.yml`` calls on its cron schedule in a deployed environment where
    the in-process APScheduler 09:00 job would never fire."""
    _require_digest_token(x_digest_token)
    summary = await scheduler_jobs.run_daily_digest_job()
    logger.info("internal.run_digest_triggered", extra={"extra_fields": summary})
    return RunDigestOut(**summary)


@router.get("/unsubscribe", response_class=HTMLResponse)
async def unsubscribe(
    user_id: uuid.UUID = Query(...),
    token: str = Query(...),
) -> HTMLResponse:
    """One-click unsubscribe from the digest email footer. No auth cookie required by design — the
    HMAC token in the link *is* the authorization (PRD §6.7: "unsubscribe via digest_optin")."""
    if not verify_unsubscribe_token(user_id, token):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Invalid unsubscribe link")

    async with AsyncSessionLocal() as session:
        result = await session.execute(
            update(User).where(User.id == user_id).values(digest_optin=False)
        )
        await session.commit()

    if result.rowcount == 0:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")

    logger.info("internal.digest_unsubscribed", extra={"extra_fields": {"user_id": str(user_id)}})
    return HTMLResponse(
        "<html><body style=\"font-family:sans-serif;padding:40px;text-align:center;\">"
        "<h2>You're unsubscribed</h2>"
        "<p>You will no longer receive the SmartReco daily digest email.</p>"
        "</body></html>"
    )
