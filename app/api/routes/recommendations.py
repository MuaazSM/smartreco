"""Recommendation surfacing + demo affordances (PRD §6.5/§6.6; IMPLEMENTATION.md Phase 7).

Three routes, all authenticated:

  * ``GET  /api/recommendations/current``  — the user's single ``is_current`` recommendation plus the
    transparency data the dashboard renders ("based on N actions · M searches · updated 2m ago" and
    the "what we noticed about you" categories). Pure read; no LLM.
  * ``POST /api/recommendations/refresh``  — a rate-limited demo button that forces a full agent run
    now. Unlike the event-ingest trigger, this is an *explicit user action*, so a synchronous LLM run
    here is intentional (it is not the "call the LLM on every event" antipattern invariant #6 guards).
    It still takes the ``gen:{user_id}`` lock, so it can never race a background regeneration.
  * ``POST /api/recommendations/feedback`` — thumbs up/down on a recommended item writes a ``feedback``
    event that feeds the *next* trigger (it counts toward ``events_since_gen`` but, not being a product
    or search event, never skews the interest centroid). The handler inserts the row fast and defers
    the profile rebuild + trigger check to a background task, mirroring the event-ingest path.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.schemas import RecommendationItem
from app.api.deps import get_current_user
from app.core.logging import get_logger, set_run_id
from app.db.models import Event, Product, User, UserProfile
from app.db.models import Recommendation as RecommendationRow
from app.db.session import AsyncSessionLocal, get_db
from app.services import cache, profile as profile_service, trigger
from app.vector.qdrant_client import QdrantVectorStore

logger = get_logger(__name__)

router = APIRouter(prefix="/api/recommendations", tags=["recommendations"])

# thumbs feedback weights (feedback is not a product/search event, so this never enters the centroid;
# it only records the signal and counts toward the next trigger).
_FEEDBACK_WEIGHTS: dict[str, float] = {"up": 2.0, "down": 0.5}


# --------------------------------------------------------------------------------------------------
# Response / request schemas
# --------------------------------------------------------------------------------------------------
class Transparency(BaseModel):
    """The "why am I seeing this" strip data for the dashboard (PRD §6.6, U4)."""

    total_events: int
    searches: int
    events_since_gen: int
    last_generated_at: datetime | None = None
    top_categories: dict[str, float] = Field(default_factory=dict)


class CurrentRecommendationOut(BaseModel):
    """The current recommendation plus its transparency context."""

    recommendation_id: str
    headline: str
    narrative: str
    items: list[RecommendationItem]
    trigger_reason: str
    created_at: datetime
    transparency: Transparency


class RefreshOut(BaseModel):
    """The result of a forced refresh."""

    recommendation_id: str | None
    cache_hit: str | None
    status: str


class FeedbackIn(BaseModel):
    """Thumbs feedback on one recommended item."""

    rec_id: uuid.UUID
    item_id: uuid.UUID  # the product the learner reacted to
    signal: str = Field(..., pattern="^(up|down)$")


class FeedbackAccepted(BaseModel):
    status: str = "recorded"


# --------------------------------------------------------------------------------------------------
# GET /current
# --------------------------------------------------------------------------------------------------
@router.get("/current", response_model=CurrentRecommendationOut)
async def current_recommendation(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> CurrentRecommendationOut:
    """Return the authenticated user's current recommendation + transparency data, or 404 if none."""
    rec = await db.scalar(
        select(RecommendationRow).where(
            RecommendationRow.user_id == current_user.id,
            RecommendationRow.is_current.is_(True),
        )
    )
    if rec is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No recommendation yet — keep browsing and one will be generated.",
        )

    total_events = await db.scalar(
        select(func.count()).select_from(Event).where(Event.user_id == current_user.id)
    )
    searches = await db.scalar(
        select(func.count())
        .select_from(Event)
        .where(Event.user_id == current_user.id, Event.event_type == "search")
    )
    prof = await db.get(UserProfile, current_user.id)

    transparency = Transparency(
        total_events=int(total_events or 0),
        searches=int(searches or 0),
        events_since_gen=int(prof.events_since_gen) if prof else 0,
        last_generated_at=prof.last_generated_at if prof else None,
        top_categories=dict(prof.top_categories or {}) if prof else {},
    )
    return CurrentRecommendationOut(
        recommendation_id=str(rec.id),
        headline=rec.headline,
        narrative=rec.narrative,
        items=[RecommendationItem(**item) for item in (rec.items or [])],
        trigger_reason=rec.trigger_reason,
        created_at=rec.created_at,
        transparency=transparency,
    )


# --------------------------------------------------------------------------------------------------
# POST /refresh  (rate-limited demo affordance)
# --------------------------------------------------------------------------------------------------
@router.post("/refresh", response_model=RefreshOut)
async def refresh_recommendation(
    current_user: User = Depends(get_current_user),
) -> RefreshOut:
    """Force a full agent run now. Rate-limited per user and serialized by the ``gen:{user_id}`` lock.

    Runs synchronously (an explicit user action, not per-event) under the agent's own 25s cap. Returns
    the new recommendation id. 429 if refreshed too recently; 409 if a regeneration is already running.
    """
    redis = cache.get_redis()
    if not await cache.refresh_allowed(
        redis, current_user.id, window_seconds=trigger.REFRESH_RATE_LIMIT_SECONDS
    ):
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Refreshed too recently — try again in a few seconds.",
        )

    token = await cache.acquire_lock(redis, current_user.id, ttl_seconds=cache.LOCK_TTL_SECONDS)
    if token is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A recommendation is already being generated for you.",
        )

    store = QdrantVectorStore()
    try:
        recommendation = await trigger.run_full_and_mark(
            current_user.id,
            trigger_reason="manual",
            vector_store=store,
            redis=redis,
        )
        logger.info(
            "recommendations.refresh",
            extra={
                "extra_fields": {
                    "user_id": str(current_user.id),
                    "run_id": getattr(recommendation, "run_id", None),
                    "status": getattr(recommendation, "status", None),
                }
            },
        )
        return RefreshOut(
            recommendation_id=getattr(recommendation, "recommendation_id", None),
            cache_hit=None,
            status=getattr(recommendation, "status", "success"),
        )
    finally:
        await cache.release_lock(redis, current_user.id, token)
        await store.close()


# --------------------------------------------------------------------------------------------------
# POST /feedback
# --------------------------------------------------------------------------------------------------
async def _feedback_followup(user_id: uuid.UUID) -> None:
    """Background: bump the profile for the just-written feedback event, then run the trigger gate."""
    set_run_id()
    store = QdrantVectorStore()
    try:
        async with AsyncSessionLocal() as session:
            await profile_service.rebuild_profile(
                session, user_id, new_event_count=1, vector_store=store
            )
        await trigger.maybe_regenerate(user_id, vector_store=store)
    except Exception as exc:  # noqa: BLE001 - background work must never crash the worker loop
        logger.error(
            "recommendations.feedback_followup_failed",
            extra={"extra_fields": {"user_id": str(user_id), "detail": f"{type(exc).__name__}: {exc}"}},
        )
    finally:
        await store.close()


@router.post(
    "/feedback", response_model=FeedbackAccepted, status_code=status.HTTP_201_CREATED
)
async def submit_feedback(
    body: FeedbackIn,
    background_tasks: BackgroundTasks,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> FeedbackAccepted:
    """Record thumbs up/down on a recommended item as a ``feedback`` event feeding the next trigger."""
    # The recommendation must belong to the caller (no cross-user feedback).
    rec = await db.get(RecommendationRow, body.rec_id)
    if rec is None or rec.user_id != current_user.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Recommendation not found")

    product = await db.get(Product, body.item_id)
    if product is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Item not found")

    event = Event(
        user_id=current_user.id,
        session_id=uuid.uuid4(),  # feedback is out-of-band; a fresh session keeps the natural key unique
        event_type="feedback",
        product_id=body.item_id,
        payload={"rec_id": str(body.rec_id), "signal": body.signal, "source": "dashboard"},
        weight=_FEEDBACK_WEIGHTS.get(body.signal, 1.0),
        client_ts=datetime.utcnow(),
    )
    db.add(event)
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Could not record feedback")

    background_tasks.add_task(_feedback_followup, current_user.id)
    logger.info(
        "recommendations.feedback",
        extra={
            "extra_fields": {
                "user_id": str(current_user.id),
                "rec_id": str(body.rec_id),
                "item_id": str(body.item_id),
                "signal": body.signal,
            }
        },
    )
    return FeedbackAccepted()
