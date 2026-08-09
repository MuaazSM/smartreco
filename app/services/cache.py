"""Redis helper for the Phase 7 cost gate (PRD §6.5): the shared async client, the per-user
regeneration lock that serializes concurrent agent runs, a demo-refresh rate limiter, and the small
"generation state" stashes (the interest vector + profile hash captured at the last generation) the
trigger policy compares against for drift-since-generation and the L1/L2 cache layers.

Redis is a cache / coordination primitive, **not** an AI SDK — nothing here touches Mesh, so this
module does not implicate invariant #1. All keys are namespaced per user so a ``DEL`` on the four
prefixes fully cleans a user's trigger state (used by tests).

Key scheme (all suffixed with the user id):

  * ``gen:{user_id}``      — the distributed regeneration lock (``SET NX EX``; value is a random token
                             released via a compare-and-delete Lua script so a caller only ever
                             releases *its own* lock, never one a later run re-acquired after TTL).
  * ``genvec:{user_id}``   — JSON interest vector at the last generation (L2 cosine + drift baseline).
  * ``genhash:{user_id}``  — ``profile_hash`` at the last generation (the L1 exact-cache key).
  * ``refresh_rl:{user_id}`` — the ``POST /api/recommendations/refresh`` rate-limit token.
"""

from __future__ import annotations

import json
import uuid

import redis.asyncio as aioredis

from app.core.config import settings
from app.core.logging import get_logger

logger = get_logger(__name__)

# Lock TTL must comfortably exceed the agent's 25s hard cap so the lock never expires mid-run and let
# a second run start; the winner always releases it explicitly in a finally long before this fires.
LOCK_TTL_SECONDS = 120
# Generation-state stashes live well beyond a day so L1/L2 stay warm across a demo session, but are
# not immortal (keeps Upstash's free-tier key count bounded).
STASH_TTL_SECONDS = 30 * 24 * 3600

_LOCK_KEY = "gen:{user_id}"
_GEN_VECTOR_KEY = "genvec:{user_id}"
_GEN_HASH_KEY = "genhash:{user_id}"
_REFRESH_RL_KEY = "refresh_rl:{user_id}"

# Compare-and-delete: only delete the lock if it still holds *our* token (never release a lock a
# later run re-acquired after ours expired). Atomic on Redis's single thread.
_RELEASE_LUA = (
    "if redis.call('get', KEYS[1]) == ARGV[1] then return redis.call('del', KEYS[1]) else return 0 end"
)

_client: aioredis.Redis | None = None


def get_redis() -> aioredis.Redis:
    """Return the process-wide async Redis client, constructing it on first use.

    ``decode_responses=True`` so ``get`` yields ``str`` (JSON/hash values), matching the stashes below.
    """
    global _client
    if _client is None:
        _client = aioredis.from_url(
            settings.redis_url, encoding="utf-8", decode_responses=True
        )
    return _client


async def close_redis() -> None:
    """Close and drop the shared client (shutdown / test teardown)."""
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None


# --------------------------------------------------------------------------------------------------
# The distributed regeneration lock (serializes concurrent agent runs for one user)
# --------------------------------------------------------------------------------------------------
async def acquire_lock(
    redis: aioredis.Redis, user_id: uuid.UUID | str, *, ttl_seconds: int = LOCK_TTL_SECONDS
) -> str | None:
    """Try to acquire ``gen:{user_id}`` with ``SET NX EX``. Returns the token on success, else ``None``.

    A ``None`` return means another run holds the lock — the caller must early-exit (this is the
    "concurrent triggers serialize" guarantee, PRD §6.5 / CLAUDE.md).
    """
    token = uuid.uuid4().hex
    acquired = await redis.set(_LOCK_KEY.format(user_id=user_id), token, nx=True, ex=ttl_seconds)
    return token if acquired else None


async def release_lock(redis: aioredis.Redis, user_id: uuid.UUID | str, token: str) -> None:
    """Release the lock iff we still hold ``token`` (compare-and-delete; best-effort, never raises)."""
    try:
        await redis.eval(_RELEASE_LUA, 1, _LOCK_KEY.format(user_id=user_id), token)
    except Exception as exc:  # noqa: BLE001 - lock release must never crash a background task
        logger.warning(
            "cache.lock_release_failed",
            extra={"extra_fields": {"user_id": str(user_id), "detail": str(exc)}},
        )


# --------------------------------------------------------------------------------------------------
# Generation-state stashes (drift-since-generation + L1/L2 cache baselines)
# --------------------------------------------------------------------------------------------------
async def stash_generation_state(
    redis: aioredis.Redis,
    user_id: uuid.UUID | str,
    *,
    interest_vector: list[float],
    profile_hash: str,
) -> None:
    """Record the interest vector + profile hash *at generation time* (the L1/L2/drift baselines)."""
    pipe = redis.pipeline()
    pipe.set(
        _GEN_VECTOR_KEY.format(user_id=user_id),
        json.dumps(list(interest_vector or [])),
        ex=STASH_TTL_SECONDS,
    )
    pipe.set(_GEN_HASH_KEY.format(user_id=user_id), profile_hash or "", ex=STASH_TTL_SECONDS)
    await pipe.execute()


async def load_generation_vector(
    redis: aioredis.Redis, user_id: uuid.UUID | str
) -> list[float] | None:
    """The interest vector at the last generation, or ``None`` if the user has never generated one."""
    raw = await redis.get(_GEN_VECTOR_KEY.format(user_id=user_id))
    if not raw:
        return None
    try:
        vector = json.loads(raw)
    except (ValueError, TypeError):
        return None
    return vector or None


async def load_generation_hash(redis: aioredis.Redis, user_id: uuid.UUID | str) -> str | None:
    """The ``profile_hash`` at the last generation, or ``None`` if never generated (the L1 key)."""
    value = await redis.get(_GEN_HASH_KEY.format(user_id=user_id))
    return value or None


async def clear_generation_state(redis: aioredis.Redis, user_id: uuid.UUID | str) -> None:
    """Drop all four per-user trigger keys (test teardown / a hard reset)."""
    await redis.delete(
        _LOCK_KEY.format(user_id=user_id),
        _GEN_VECTOR_KEY.format(user_id=user_id),
        _GEN_HASH_KEY.format(user_id=user_id),
        _REFRESH_RL_KEY.format(user_id=user_id),
    )


# --------------------------------------------------------------------------------------------------
# Demo-refresh rate limit
# --------------------------------------------------------------------------------------------------
async def refresh_allowed(
    redis: aioredis.Redis, user_id: uuid.UUID | str, *, window_seconds: int
) -> bool:
    """Token-bucket-of-one for ``POST /refresh``: ``True`` if allowed, ``False`` if within the window.

    A manual refresh forces a full (paid) agent run, so it is rate-limited to protect Mesh quota.
    """
    acquired = await redis.set(
        _REFRESH_RL_KEY.format(user_id=user_id), "1", nx=True, ex=window_seconds
    )
    return bool(acquired)
