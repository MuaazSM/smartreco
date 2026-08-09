"""Behavioral event bulk-ingest with server-side weighting (PRD §6.3 + Figure 4; CLAUDE.md #6).

This is the write half of the tracking pipeline. It is deliberately **LLM/embedding-free** — the
ingest path never calls Mesh (invariant #6 adjacent): it does one bulk ``INSERT ... ON CONFLICT DO
NOTHING`` and returns. The profile recompute (``app/services/profile.py``) and any regeneration
trigger (Phase 7) run *after* this, off the hot request path.

Two invariants live here:

  * **Server-assigned weight.** The client never supplies a weight — it is derived here from the
    event type (view 1.0 · click 1.5 · search 2.0 · dwell>30s 2.5 · cart 3.0). A client-supplied
    weight, if any leaked through, is ignored. This is what keeps interest scoring honest (PRD §6.3
    "Trust").
  * **Natural-key idempotency.** ``events`` has ``UNIQUE (user_id, session_id, event_type,
    client_ts)``; a retried ``sendBeacon`` batch (or a ``localStorage`` spillover replayed on next
    load) re-sends the same rows, and ``ON CONFLICT DO NOTHING`` makes the retry a no-op. The
    client-generated event UUID is carried through in ``payload.event_id`` for cross-referencing, but
    the *table's* natural key — not that UUID — is the server-side dedupe.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.db.models import Event

logger = get_logger(__name__)

# PRD §6.3 server-side event weights. The only place weight is decided — never the client.
BASE_WEIGHTS: dict[str, float] = {
    "view": 1.0,
    "click": 1.5,
    "search": 2.0,
    "dwell": 2.5,
    "cart": 3.0,
}

# The set of event types the ingest endpoint accepts. Anything else is a 422 at the route boundary.
ALLOWED_EVENT_TYPES: frozenset[str] = frozenset(BASE_WEIGHTS)

# A "dwell" only earns the full 2.5 weight when foreground time exceeds this threshold (PRD §6.3
# "dwell>30s 2.5"); a brief glance is scored like a plain view so a flicker of attention can't
# masquerade as strong intent.
DWELL_STRONG_MS = 30_000
_DWELL_WEAK_WEIGHT = 1.0

# The natural-key columns backing UNIQUE(user_id, session_id, event_type, client_ts).
_NATURAL_KEY = ("user_id", "session_id", "event_type", "client_ts")


@dataclass(frozen=True, slots=True)
class IncomingEvent:
    """One validated, not-yet-persisted event. ``weight`` is assigned here, never carried in."""

    session_id: uuid.UUID
    event_type: str
    client_ts: datetime
    product_id: uuid.UUID | None = None
    payload: dict | None = None


def weight_for(event_type: str, payload: dict | None) -> float:
    """The server-assigned weight for an event (PRD §6.3). Client input never influences this.

    ``dwell`` is the one type whose weight is payload-sensitive: a dwell over ``DWELL_STRONG_MS`` of
    foreground time scores 2.5, a shorter one scores like a view (1.0). Every other type is a flat
    lookup. An unknown type (should never reach here — the route validates) defaults to 1.0.
    """
    if event_type == "dwell":
        dwell_ms = _dwell_ms(payload)
        return BASE_WEIGHTS["dwell"] if dwell_ms > DWELL_STRONG_MS else _DWELL_WEAK_WEIGHT
    return BASE_WEIGHTS.get(event_type, 1.0)


def _dwell_ms(payload: dict | None) -> float:
    """Best-effort foreground-dwell milliseconds from the payload (``dwell_ms`` or ``dwell_seconds``)."""
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


def _dedupe_within_batch(
    user_id: uuid.UUID, events: list[IncomingEvent]
) -> list[dict]:
    """Build the INSERT rows, dropping intra-batch natural-key collisions first.

    ``ON CONFLICT DO NOTHING`` handles collisions against *existing* rows; de-duplicating within the
    batch here keeps the statement unambiguous and the returned insert-count meaningful.
    """
    seen: set[tuple] = set()
    rows: list[dict] = []
    for event in events:
        key = (user_id, event.session_id, event.event_type, event.client_ts)
        if key in seen:
            continue
        seen.add(key)
        rows.append(
            {
                "user_id": user_id,
                "session_id": event.session_id,
                "event_type": event.event_type,
                "product_id": event.product_id,
                "payload": event.payload or {},
                "weight": weight_for(event.event_type, event.payload),
                "client_ts": event.client_ts,
            }
        )
    return rows


async def ingest_events(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    events: list[IncomingEvent],
) -> int:
    """Bulk-insert a batch of events for ``user_id`` and return how many rows were *newly* inserted.

    Server-assigns every ``weight``; de-duplicates the natural key within the batch and against the
    table (``ON CONFLICT DO NOTHING``), so a retried/replayed batch inserts 0 new rows. Commits the
    batch in a single statement. Makes **no** LLM/embedding call — that would violate invariant #6.
    """
    if not events:
        return 0

    rows = _dedupe_within_batch(user_id, events)
    stmt = (
        pg_insert(Event)
        .values(rows)
        .on_conflict_do_nothing(index_elements=list(_NATURAL_KEY))
        .returning(Event.id)
    )
    result = await session.execute(stmt)
    inserted = len(result.scalars().all())
    await session.commit()

    logger.info(
        "events.ingested",
        extra={
            "extra_fields": {
                "user_id": str(user_id),
                "submitted": len(events),
                "inserted": inserted,
                "deduped": len(events) - inserted,
            }
        },
    )
    return inserted
