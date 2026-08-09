"""Behavioral event ingest endpoint (PRD §6.3 + Figure 4; IMPLEMENTATION.md Phase 5).

``POST /api/events/batch`` is the single write surface for the tracker (``frontend/lib/tracker.ts``).
Its contract is deliberately narrow and fast:

  * **Authenticated.** ``user_id`` is taken from the JWT-cookie session (``get_current_user``), never
    from the request body — a client cannot attribute events to another user.
  * **No client weight.** The body has no ``weight`` field; the server assigns it by event type
    (``app.services.event_ingest``) so interest scoring never trusts the client (PRD §6.3 "Trust").
  * **202 Accepted, then background work.** The handler validates, schedules a ``BackgroundTask``, and
    returns ``202`` immediately. The bulk insert *and* the profile recompute run **after** the
    response — the request path itself does no heavy work and, critically, **never** calls the
    LLM/embeddings (invariant #6). A regeneration trigger (Phase 7) hangs off the same background
    step, still off the hot path.
  * **Idempotent.** Retried/replayed batches dedupe on the ``events`` natural key (``ON CONFLICT DO
    NOTHING``); the client-generated per-event UUID rides along in ``payload.event_id``.

The background task opens its **own** ``AsyncSession`` (the request-scoped one from ``get_db`` is
already closed by the time it runs) and its own read-only Qdrant store for the centroid.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from fastapi import APIRouter, BackgroundTasks, Depends, status
from pydantic import BaseModel, Field, field_validator

from app.api.deps import get_current_user
from app.core.logging import get_logger, set_run_id
from app.db.models import User
from app.db.session import AsyncSessionLocal
from app.services import event_ingest, profile
from app.services.event_ingest import ALLOWED_EVENT_TYPES, IncomingEvent
from app.vector.qdrant_client import QdrantVectorStore

logger = get_logger(__name__)

router = APIRouter(prefix="/api/events", tags=["events"])

# A single beacon/flush carries at most a couple dozen events (tracker flushes at 20); a replayed
# localStorage spillover may be larger. Cap generously to bound one request's work.
_MAX_BATCH = 1000


class EventIn(BaseModel):
    """One tracked event as sent by the client. Note the absence of any ``weight`` field."""

    session_id: uuid.UUID
    event_type: str = Field(..., max_length=32)
    client_ts: datetime
    product_id: uuid.UUID | None = None
    event_id: uuid.UUID | None = None  # client-generated idempotency id; stored in payload
    payload: dict = Field(default_factory=dict)

    @field_validator("event_type")
    @classmethod
    def _known_event_type(cls, value: str) -> str:
        if value not in ALLOWED_EVENT_TYPES:
            raise ValueError(
                f"unknown event_type {value!r}; expected one of {sorted(ALLOWED_EVENT_TYPES)}"
            )
        return value


class EventBatchIn(BaseModel):
    """The batch envelope. ``session_id`` may vary per event (a beacon can span sessions)."""

    events: list[EventIn] = Field(..., min_length=1, max_length=_MAX_BATCH)


class BatchAccepted(BaseModel):
    """202 body — acknowledges receipt; persistence happens asynchronously."""

    accepted: int
    status: str = "accepted"


def _to_incoming(event: EventIn) -> IncomingEvent:
    """Fold the client UUID into the payload and drop to the weight-free internal event shape."""
    payload = dict(event.payload)
    if event.event_id is not None:
        payload.setdefault("event_id", str(event.event_id))
    return IncomingEvent(
        session_id=event.session_id,
        event_type=event.event_type,
        client_ts=event.client_ts,
        product_id=event.product_id,
        payload=payload,
    )


async def _persist_and_profile(user_id: uuid.UUID, incoming: list[IncomingEvent]) -> None:
    """Background step: bulk-insert the batch, then recompute the profile. Never raises to the caller.

    Owns its own DB session and Qdrant store. LLM-free by construction — ingest does one SQL insert
    and the profile centroid averages pre-computed Qdrant vectors. A trigger check (Phase 7) will be
    invoked from here, after the batch is persisted.
    """
    set_run_id()
    store = QdrantVectorStore()
    try:
        async with AsyncSessionLocal() as session:
            inserted = await event_ingest.ingest_events(
                session, user_id=user_id, events=incoming
            )
            if inserted:
                await profile.rebuild_profile(
                    session,
                    user_id,
                    new_event_count=inserted,
                    vector_store=store,
                )
    except Exception as exc:  # noqa: BLE001 - background work must never crash the worker loop
        logger.error(
            "events.background_failed",
            extra={"extra_fields": {"user_id": str(user_id), "detail": f"{type(exc).__name__}: {exc}"}},
        )
    finally:
        await store.close()


@router.post("/batch", response_model=BatchAccepted, status_code=status.HTTP_202_ACCEPTED)
async def ingest_batch(
    body: EventBatchIn,
    background_tasks: BackgroundTasks,
    current_user: User = Depends(get_current_user),
) -> BatchAccepted:
    """Accept a batch of tracked events for the authenticated user and process it in the background.

    Returns ``202`` immediately; the bulk insert + profile recompute run after the response. Does no
    LLM/embedding work on the request path (invariant #6).
    """
    incoming = [_to_incoming(event) for event in body.events]
    background_tasks.add_task(_persist_and_profile, current_user.id, incoming)
    logger.info(
        "events.batch_accepted",
        extra={"extra_fields": {"user_id": str(current_user.id), "count": len(incoming)}},
    )
    return BatchAccepted(accepted=len(incoming))
