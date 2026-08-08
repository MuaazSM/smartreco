"""SQLAlchemy ORM models for the full SmartReco schema (PRD §5 "Data model" + "Key DDL details").

Schema is created exclusively through Alembic (`app/db/migrations/`) — nothing here ever calls
`Base.metadata.create_all()`. These classes exist so the rest of the app gets typed, autocompletable
row objects; the initial migration under `app/db/migrations/versions/` is hand-written to match this
file field-for-field (Alembic autogenerate cannot express partial indexes, `INCLUDE` covering
indexes, or the `WHERE is_current` unique index used below, so both are maintained by hand and must
be kept in sync manually when either changes).

Seven tables, matching the PRD §5 ER model and IMPLEMENTATION.md Phase 1 exactly:
`users`, `user_profiles`, `products`, `events`, `vector_outbox`, `agent_runs`, `recommendations`.

Conventions:
- UUID primary keys default server-side via `gen_random_uuid()` (pgcrypto), except `events`
  (`BIGSERIAL`, per the PRD's literal DDL — a high-volume append-only log benefits from a narrower,
  monotonic key) and `vector_outbox` (`BIGSERIAL`, same reasoning, and it mirrors the PRD DDL).
- `users.email` is `CITEXT` so `Foo@Example.com` and `foo@example.com` collide on the unique
  constraint — sign-up/login must not be case-sensitive on email.
- All timestamps are `server_default=text("now()")`, never a Python-side `datetime.utcnow` default,
  per CLAUDE.md DB conventions.
- Every function signature is typed; models use SQLAlchemy 2.0 `Mapped`/`mapped_column` style.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    Numeric,
    REAL,
    Text,
    UniqueConstraint,
    desc,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, CITEXT, JSONB, TIMESTAMP
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

# A fixed naming convention keeps constraint/index names deterministic across the hand-written
# migration and any future autogenerate diff, and gives Postgres error messages a readable name
# instead of an auto-numbered one.
_NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=_NAMING_CONVENTION)


def _uuid_pk() -> Mapped[uuid.UUID]:
    return mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )


class User(Base):
    """A learner or admin. PRD §6.1 — email + bcrypt password, JWT with a `role` claim."""

    __tablename__ = "users"

    id: Mapped[uuid.UUID] = _uuid_pk()
    email: Mapped[str] = mapped_column(CITEXT, nullable=False, unique=True)
    password_hash: Mapped[str] = mapped_column(Text, nullable=False)
    display_name: Mapped[str] = mapped_column(Text, nullable=False)
    role: Mapped[str] = mapped_column(Text, nullable=False, server_default="user")
    # Opt-in for the 09:00 daily digest (PRD §6.7); unsubscribe flips this off.
    digest_optin: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=text("now()")
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=text("now()")
    )

    profile: Mapped["UserProfile | None"] = relationship(
        back_populates="user", uselist=False, cascade="all, delete-orphan"
    )

    __table_args__ = (CheckConstraint("role IN ('user','admin')", name="role_valid"),)


class Product(Base):
    """A course row. PRD §6.2 — the Postgres side of the dual-write; Qdrant holds the vector twin.

    `content_hash` + `vector_synced_at` back the "skip re-embed when unchanged" optimization and
    the `/api/admin/sync-status` drift audit (PRD §6.2). Delete is always a soft-delete
    (`is_active=false`) plus a `vector_outbox` delete op — rows are never hard-deleted here.
    """

    __tablename__ = "products"

    id: Mapped[uuid.UUID] = _uuid_pk()
    title: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    category: Mapped[str] = mapped_column(Text, nullable=False)
    tags: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False, server_default=text("'{}'"))
    level: Mapped[str] = mapped_column(Text, nullable=False)
    price_cents: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    # Hash of the embeddable content (title + description + tags, ...); the outbox worker (Phase 4)
    # recomputes this on update and skips re-embedding when it hasn't changed.
    content_hash: Mapped[str | None] = mapped_column(Text, nullable=True)
    vector_synced_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=text("now()")
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=text("now()")
    )

    __table_args__ = (
        CheckConstraint("level IN ('beginner','intermediate','advanced')", name="level_valid"),
        CheckConstraint("price_cents >= 0", name="price_cents_nonnegative"),
        Index("ix_products_category", "category"),
        Index("ix_products_is_active", "is_active"),
    )


class UserProfile(Base):
    """The incrementally-updated behavioral profile (PRD §6.3, `app/services/profile.py`).

    One row per user (PK = `user_id`). `interest_vector` is the decayed interest centroid (3-day
    half-life); `top_categories`/`top_terms` are JSONB maps of label → weight. `profile_hash` is the
    L1 exact-cache key for the trigger policy (`app/services/trigger.py`); `events_since_gen` is the
    "8+ new events" counter that resets on every agent run.

    Deliberately NOT stored here: level affinity and "products already seen" — `build_profile`
    (the agent's first, no-LLM node) derives those live from `events` on each run rather than
    persisting a second, easily-stale copy. Keeping this row to the PRD's explicit field list keeps
    the L1 cache key (`profile_hash`) meaningful: it should change exactly when the decayed
    centroid/top-categories/top-terms move, not whenever an unrelated event lands.
    """

    __tablename__ = "user_profiles"

    user_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    interest_vector: Mapped[list[float]] = mapped_column(
        ARRAY(REAL), nullable=False, server_default=text("'{}'")
    )
    top_categories: Mapped[dict] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    top_terms: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    events_since_gen: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    profile_hash: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_generated_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=text("now()")
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=text("now()")
    )

    user: Mapped["User"] = relationship(back_populates="profile")


class Event(Base):
    """A single tracked behavioral event (PRD §6.3 + "Key DDL details", reproduced verbatim).

    `weight` is always server-assigned (`app/services/event_ingest.py`) — a client-supplied weight
    is never trusted. The `(user_id, session_id, event_type, client_ts)` unique constraint is what
    makes a retried `sendBeacon` batch idempotent; do not remove it.
    """

    __tablename__ = "events"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    user_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    session_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    event_type: Mapped[str] = mapped_column(Text, nullable=False)
    product_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("products.id", ondelete="SET NULL"), nullable=True
    )
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    weight: Mapped[float] = mapped_column(REAL, nullable=False, server_default="1.0")
    client_ts: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False)
    server_ts: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=text("now()")
    )

    __table_args__ = (
        UniqueConstraint(
            "user_id", "session_id", "event_type", "client_ts", name="user_id_session_id_event_type_client_ts"
        ),
        Index("ix_events_product_id", "product_id"),
    )


# `(user_id, server_ts DESC)` per PRD "Key DDL details" — declared outside __table_args__ because
# expressing a DESC column ordering requires the actual column object, not a string, and the class
# body can't self-reference `Event.server_ts` while it's still being defined. The partial covering
# index (`INCLUDE (event_type, product_id) WHERE server_ts > ...`) has no SQLAlchemy `Index()`
# equivalent at all (partial + INCLUDE together) and is hand-written directly in the migration.
Index("ix_events_user_id_server_ts", Event.user_id, desc(Event.server_ts))


class VectorOutbox(Base):
    """The transactional outbox (PRD §6.2 Figure 3 + "Key DDL details", reproduced verbatim).

    `app/services/catalog.py` inserts a row here in the *same transaction* as the `products` write.
    `app/services/outbox_worker.py` drains it every 5s with `SELECT ... FOR UPDATE SKIP LOCKED`.
    `product_id` intentionally carries no FK — a `delete` op must still be replayable after the
    referenced product row concept has been retired, and this table is the append-only ledger of
    intent, not a live join target.
    """

    __tablename__ = "vector_outbox"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    product_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    op: Mapped[str] = mapped_column(Text, nullable=False)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="pending")
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=text("now()")
    )
    processed_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)

    __table_args__ = (
        CheckConstraint("op IN ('upsert','delete')", name="op_valid"),
        CheckConstraint("status IN ('pending','done','failed')", name="status_valid"),
        # The one index this table needs — "WHERE status = 'pending'" — is partial, which
        # SQLAlchemy's `Index()` can express (`postgresql_where=...`) but which is instead
        # hand-written directly in the migration alongside this table's other hand-written indexes,
        # to keep every non-trivial index for this schema defined in exactly one place.
    )


class AgentRun(Base):
    """One LangGraph execution (PRD §6.4 + §6.8). `node_path` is what proves the graph is real.

    `run_id` is the log-correlation id minted by `app.core.logging.new_run_id()` / `set_run_id()`
    (a bare hex uuid4) — kept as its own unique text column, separate from the surrogate `id` PK, so
    a structured log line can be grepped straight back to this row without assuming the log's id
    format matches Postgres's canonical hyphenated UUID text representation.
    `cache_hit` records which trigger-policy cache layer served the request (PRD §6.5,
    `app/services/trigger.py`): `'l1'` (exact profile-hash match), `'l2'` (semantic cache), or NULL
    for a full graph run.
    """

    __tablename__ = "agent_runs"

    id: Mapped[uuid.UUID] = _uuid_pk()
    run_id: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    user_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    trigger_reason: Mapped[str] = mapped_column(Text, nullable=False)
    node_path: Mapped[list[str]] = mapped_column(
        ARRAY(Text), nullable=False, server_default=text("'{}'")
    )
    retrieval_score: Mapped[float | None] = mapped_column(REAL, nullable=True)
    refine_loops: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    cache_hit: Mapped[str | None] = mapped_column(Text, nullable=True)
    models_used: Mapped[dict] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    cost_usd: Mapped[float] = mapped_column(Numeric(10, 6), nullable=False, server_default="0")
    latency_ms: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    langsmith_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="running")
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=text("now()")
    )
    completed_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)

    __table_args__ = (
        CheckConstraint(
            "cache_hit IS NULL OR cache_hit IN ('l1','l2')", name="cache_hit_valid"
        ),
        CheckConstraint(
            "status IN ('running','success','failed','fallback')", name="status_valid"
        ),
        Index("ix_agent_runs_user_id_created_at", "user_id", "created_at"),
    )


class Recommendation(Base):
    """A generated recommendation (PRD §6.4 node 6 output + §6.6 dashboard rendering).

    `items` is the validated `[{product_id, reason}]` list from `validate_grounding` — every
    `product_id` in it is guaranteed, by the graph's structural gate, to reference a real, active
    `products` row. Exactly one row per user may have `is_current=true`, enforced by the
    `WHERE is_current` partial unique index (hand-written in the migration): the surfacing route
    reads that row directly with no ORDER BY/LIMIT tie-breaking required.
    """

    __tablename__ = "recommendations"

    id: Mapped[uuid.UUID] = _uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    agent_run_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("agent_runs.id", ondelete="SET NULL"), nullable=True
    )
    headline: Mapped[str] = mapped_column(Text, nullable=False)
    narrative: Mapped[str] = mapped_column(Text, nullable=False)
    items: Mapped[list] = mapped_column(JSONB, nullable=False)
    trigger_reason: Mapped[str] = mapped_column(Text, nullable=False)
    is_current: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=text("now()")
    )

    __table_args__ = (
        # The actual partial unique index ("WHERE is_current" only, no explicit ix name collision
        # with a plain non-partial index) is hand-written in the migration — see note above.
        Index("ix_recommendations_user_id_created_at", "user_id", "created_at"),
    )
