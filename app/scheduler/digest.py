"""The 09:00 daily digest email: content generation, HTML rendering, and delivery (PRD §6.7 / F7).

**Content is the agent's, not a template's invention.** ``build_digest_for_user`` calls
``app.services.trigger.run_full_and_mark(..., trigger_reason="scheduled")`` — the exact same
grounded, seven-node LangGraph path (Mesh-only, invariant #1) that powers the live dashboard. This
module never talks to an LLM directly; it only formats what the agent already produced. The one
piece of copy this module *does* generate itself is the "hook" line ("Yesterday you spent ~20
minutes exploring Advanced RAG...") — a pure, LLM-free aggregation over the user's last-24h
``events`` (mirrors the display-only slice of ``app/services/event_ingest.py``'s dwell-weight logic;
duplicated narrowly here rather than importing that module's private helper).

**Delivery degrades gracefully.** ``send_email`` tries SMTP (stdlib ``smtplib``/``email`` — no new
dependency) first, then Resend (a plain ``httpx`` POST — also no new dependency; the ``resend`` PyPI
package is deliberately not added), and if neither is configured it logs and returns
``sent=False``. Callers (``app/scheduler/jobs.py``, ``scripts/send_digest_now.py``) then persist the
rendered HTML to disk via ``save_html_to_file`` so a demo/local run never silently loses the content
just because ``.env`` ships blank ``SMTP_*``/``RESEND_API_KEY`` placeholders (CLAUDE.md invariant #4).

**Unsubscribe** (``verify_unsubscribe_token`` / ``unsubscribe_url``) is an HMAC-SHA256 of the user id
keyed on the already-existing ``settings.jwt_secret`` — no new secret, no schema change — so a click
from an email client (no session cookie present) can safely flip ``users.digest_optin`` off via
``app/api/routes/internal.py``'s ``GET /api/internal/unsubscribe``. That route imports the two
functions from here (not the other way around) to keep the import graph acyclic: ``internal.py`` and
``jobs.py`` both depend on this module; this module depends on neither.

**Telegram** is the optional second channel PRD §6.7 mentions. Since neither ``app/core/config.py``
nor the ``users`` table has a per-user chat-id column (both off-limits to this phase), it is wired as
a single admin/demo notification channel read straight from ``TELEGRAM_BOT_TOKEN`` /
``TELEGRAM_CHAT_ID`` env vars (skipped entirely, no error, when unset) rather than a per-recipient
digest channel — consistent with it being first in the PRD's bonus cut-order.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import os
import smtplib
import tempfile
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from pathlib import Path

import httpx
from jinja2 import Environment, FileSystemLoader, select_autoescape
from sqlalchemy import select

from app.agent import run_agent
from app.core.config import settings
from app.core.logging import get_logger
from app.db.models import Event, Product, User
from app.db.session import AsyncSessionLocal
from app.services import cache
from app.services.trigger import run_full_and_mark

logger = get_logger(__name__)

_TEMPLATES_DIR = Path(__file__).parent / "templates"
_env = Environment(
    loader=FileSystemLoader(str(_TEMPLATES_DIR)),
    autoescape=select_autoescape(["html", "j2"]),
)

_DEFAULT_HOOK = "Here's a fresh look at courses that match what you've been exploring."

# app/core/config.py is off-limits to this phase (owned by earlier phases) and there is no
# "public backend base URL" field on Settings — split-deployment topologies can override this via a
# plain env var without a schema/config change; same-origin local dev needs nothing set at all.
_BACKEND_BASE_URL = os.getenv("BACKEND_BASE_URL", "http://localhost:8000").rstrip("/")

_DIGEST_DIR_NAME = "smartreco_digests"


@dataclass(slots=True)
class DigestResult:
    """What one user's digest attempt produced — used by both the scheduled job and the demo script."""

    user_id: str
    email: str
    headline: str
    html: str
    hook: str
    run_id: str
    sent: bool = False
    channel: str | None = None  # "smtp" | "resend" | None
    saved_path: str | None = None  # set when not sent, so the content is never just lost
    skipped: bool = False
    skip_reason: str | None = None  # e.g. "locked" — a concurrent regen was already in flight


# --------------------------------------------------------------------------------------------------
# Unsubscribe token — HMAC(user_id) keyed on the existing JWT secret; no new secret, no schema change
# --------------------------------------------------------------------------------------------------
def _unsubscribe_token(user_id: uuid.UUID | str) -> str:
    return hmac.new(
        settings.jwt_secret.encode("utf-8"), str(user_id).encode("utf-8"), hashlib.sha256
    ).hexdigest()


def verify_unsubscribe_token(user_id: uuid.UUID | str, token: str) -> bool:
    return hmac.compare_digest(_unsubscribe_token(user_id), token or "")


def unsubscribe_url(user_id: uuid.UUID | str, *, base_url: str | None = None) -> str:
    """The signed one-click unsubscribe link embedded in the digest email (PRD §6.7)."""
    base = (base_url or _BACKEND_BASE_URL).rstrip("/")
    return f"{base}/api/internal/unsubscribe?user_id={user_id}&token={_unsubscribe_token(user_id)}"


# --------------------------------------------------------------------------------------------------
# The hook — a pure, LLM-free recap of the last 24h (PRD §6.7's "Yesterday you spent 20 minutes...")
# --------------------------------------------------------------------------------------------------
def _dwell_ms(payload: dict | None) -> float:
    """Mirrors ``app/services/event_ingest.py::_dwell_ms`` narrowly for display purposes only — this
    number never feeds a trigger/weighting decision, so a small, local duplicate beats importing a
    private helper from a module this phase does not own."""
    if not payload:
        return 0.0
    if "dwell_ms" in payload:
        try:
            return float(payload["dwell_ms"])
        except (TypeError, ValueError):
            return 0.0
    if "dwell_seconds" in payload:
        try:
            return float(payload["dwell_seconds"]) * 1000.0
        except (TypeError, ValueError):
            return 0.0
    return 0.0


async def _build_hook(session_factory, user_id: uuid.UUID, now: datetime) -> str:
    """The strongest category (by server-assigned event weight) in the last 24h, with a dwell-time
    callout when we have one — real, grounded activity, never invented copy."""
    since = now - timedelta(hours=24)
    async with session_factory() as session:
        stmt = (
            select(Event.event_type, Event.weight, Event.payload, Product.category)
            .join(Product, Product.id == Event.product_id)
            .where(Event.user_id == user_id, Event.server_ts >= since)
        )
        rows = (await session.execute(stmt)).all()

    if not rows:
        return _DEFAULT_HOOK

    scores: dict[str, float] = {}
    dwell_ms_by_category: dict[str, float] = {}
    for event_type, weight, payload, category in rows:
        scores[category] = scores.get(category, 0.0) + float(weight or 0.0)
        if event_type == "dwell":
            dwell_ms_by_category[category] = dwell_ms_by_category.get(category, 0.0) + _dwell_ms(
                payload
            )

    top_category = max(scores, key=lambda key: scores[key])
    dwell_minutes = dwell_ms_by_category.get(top_category, 0.0) / 60_000.0
    if dwell_minutes >= 1:
        return (
            f"Yesterday you spent about {dwell_minutes:.0f} minutes exploring {top_category}. "
            "Here's the natural next step."
        )
    return f"You've been exploring {top_category} recently. Here's the natural next step."


# --------------------------------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------------------------------
def render_digest_html(*, user: User, recommendation, hook: str, now: datetime) -> str:
    """Jinja2 HTML recap-with-hook (PRD §6.7). ``recommendation`` is the ``app.agent.Recommendation``
    ``run_full_and_mark`` returned — already grounded (invariant #2), never re-validated here."""
    template = _env.get_template("digest.html.j2")
    items = [
        item.model_dump() if hasattr(item, "model_dump") else dict(item)
        for item in recommendation.items
    ]
    return template.render(
        display_name=user.display_name,
        headline=recommendation.headline,
        narrative=recommendation.narrative,
        hook=hook,
        items=items,
        trigger_reason=recommendation.trigger_reason,
        generated_at=now.strftime("%Y-%m-%d %H:%M UTC"),
        unsubscribe_url=unsubscribe_url(user.id),
    )


# --------------------------------------------------------------------------------------------------
# Delivery — SMTP (stdlib) then Resend (httpx); no new dependency either way
# --------------------------------------------------------------------------------------------------
def _send_via_smtp_sync(to_email: str, subject: str, html: str) -> None:
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = settings.smtp_from or settings.smtp_user or "smartreco@example.com"
    message["To"] = to_email
    message.set_content("This email requires an HTML-capable client to view the SmartReco digest.")
    message.add_alternative(html, subtype="html")

    with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=10) as server:
        server.starttls()
        if settings.smtp_user and settings.smtp_password:
            server.login(settings.smtp_user, settings.smtp_password)
        server.send_message(message)


async def _send_via_resend(to_email: str, subject: str, html: str) -> None:
    async with httpx.AsyncClient(timeout=10.0) as client:
        response = await client.post(
            "https://api.resend.com/emails",
            headers={"Authorization": f"Bearer {settings.resend_api_key}"},
            json={
                "from": settings.smtp_from or "SmartReco <onboarding@resend.dev>",
                "to": [to_email],
                "subject": subject,
                "html": html,
            },
        )
        response.raise_for_status()


async def send_email(to_email: str, subject: str, html: str) -> tuple[bool, str | None]:
    """Best-effort send. Never raises — a delivery failure or missing creds returns ``(False, None)``
    and logs, exactly like this codebase's other "must never crash the caller" boundaries (Qdrant
    bootstrap, model-catalog refresh, health checks)."""
    if settings.smtp_host:
        try:
            await asyncio.to_thread(_send_via_smtp_sync, to_email, subject, html)
            return True, "smtp"
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "digest.smtp_send_failed",
                extra={"extra_fields": {"to": to_email, "detail": f"{type(exc).__name__}: {exc}"}},
            )
            return False, None

    if settings.resend_api_key:
        try:
            await _send_via_resend(to_email, subject, html)
            return True, "resend"
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "digest.resend_send_failed",
                extra={"extra_fields": {"to": to_email, "detail": f"{type(exc).__name__}: {exc}"}},
            )
            return False, None

    logger.info(
        "digest.email_not_sent",
        extra={"extra_fields": {"to": to_email, "reason": "no SMTP/Resend configured"}},
    )
    return False, None


async def send_telegram_notification(message: str) -> bool:
    """Optional second channel (PRD §6.7). Skipped entirely — no error, no log noise — unless both
    ``TELEGRAM_BOT_TOKEN`` and ``TELEGRAM_CHAT_ID`` are set (read directly: ``app/core/config.py`` is
    off-limits to this phase, and per-user chat ids have no home in the current schema)."""
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        return False
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={"chat_id": chat_id, "text": message},
            )
            response.raise_for_status()
        return True
    except Exception as exc:  # noqa: BLE001 - a notification failure must never break the caller
        logger.error(
            "digest.telegram_send_failed",
            extra={"extra_fields": {"detail": f"{type(exc).__name__}: {exc}"}},
        )
        return False


def save_html_to_file(html: str, user: User, *, save_dir: Path | None = None) -> Path:
    """Persist the rendered digest when it could not be emailed, so content is never just lost
    (graceful-degradation contract shared by the job and ``scripts/send_digest_now.py``)."""
    directory = save_dir or (Path(tempfile.gettempdir()) / _DIGEST_DIR_NAME)
    directory.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = directory / f"digest_{user.id}_{stamp}.html"
    path.write_text(html, encoding="utf-8")
    return path


# --------------------------------------------------------------------------------------------------
# The two public entry points jobs.py / send_digest_now.py call
# --------------------------------------------------------------------------------------------------
async def build_digest_for_user(
    user: User,
    *,
    session_factory=AsyncSessionLocal,
    vector_store=None,
    redis=None,
    run_agent_fn=run_agent,
    now: datetime | None = None,
) -> DigestResult:
    """Generate (agent, Mesh-only) + render the digest for one user. Does **not** send — callers that
    also want delivery should use ``send_digest_for_user``; this is exposed separately so tests and
    ``send_digest_now.py --dry-run``-style flows can inspect content without touching SMTP/Resend."""
    now = now or datetime.now(timezone.utc)
    hook = await _build_hook(session_factory, user.id, now)
    recommendation = await run_full_and_mark(
        user.id,
        trigger_reason="scheduled",
        session_factory=session_factory,
        vector_store=vector_store,
        redis=redis,
        run_agent_fn=run_agent_fn,
        now=now,
    )
    html = render_digest_html(user=user, recommendation=recommendation, hook=hook, now=now)
    return DigestResult(
        user_id=str(user.id),
        email=user.email,
        headline=recommendation.headline,
        html=html,
        hook=hook,
        run_id=getattr(recommendation, "run_id", ""),
    )


async def send_digest_for_user(
    user: User,
    *,
    session_factory=AsyncSessionLocal,
    vector_store=None,
    redis=None,
    run_agent_fn=run_agent,
    now: datetime | None = None,
    save_on_no_send: bool = True,
    save_dir: Path | None = None,
) -> DigestResult:
    """Build + attempt delivery for one user, serialized against a concurrent trigger-policy run via
    the same ``gen:{user_id}`` Redis lock ``app/services/trigger.py`` uses (PRD §6.5) — a scheduled
    digest must never race a live event-driven regeneration for the same user."""
    now = now or datetime.now(timezone.utc)
    redis_client = redis or cache.get_redis()

    token = await cache.acquire_lock(redis_client, user.id, ttl_seconds=cache.LOCK_TTL_SECONDS)
    if token is None:
        logger.info(
            "digest.user_skipped_locked", extra={"extra_fields": {"user_id": str(user.id)}}
        )
        return DigestResult(
            user_id=str(user.id),
            email=user.email,
            headline="",
            html="",
            hook="",
            run_id="",
            skipped=True,
            skip_reason="locked",
        )

    try:
        result = await build_digest_for_user(
            user,
            session_factory=session_factory,
            vector_store=vector_store,
            redis=redis_client,
            run_agent_fn=run_agent_fn,
            now=now,
        )
    finally:
        await cache.release_lock(redis_client, user.id, token)

    sent, channel = await send_email(
        user.email, subject=f"{result.headline} — your SmartReco digest", html=result.html
    )
    result.sent = sent
    result.channel = channel
    if not sent and save_on_no_send:
        path = save_html_to_file(result.html, user, save_dir=save_dir)
        result.saved_path = str(path)
        logger.info(
            "digest.saved_to_disk",
            extra={"extra_fields": {"user_id": str(user.id), "path": str(path)}},
        )
    return result
