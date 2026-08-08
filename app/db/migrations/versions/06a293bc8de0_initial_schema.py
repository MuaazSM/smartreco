"""initial schema

Full SmartReco schema (PRD §5 "Data model" + "Key DDL details", IMPLEMENTATION.md Phase 1):
`users`, `products`, `user_profiles`, `events`, `vector_outbox`, `agent_runs`, `recommendations`.

Every `CREATE TABLE` / plain `CREATE INDEX` statement below is a frozen, byte-for-byte copy of what
`sqlalchemy.schema.CreateTable` / `CreateIndex` compile from `app/db/models.py` against the
postgresql dialect at the time this migration was written (verified by hand, not regenerated at
migration-run-time) — this migration does NOT import `app.db.models` at runtime. A migration must be
a frozen snapshot of DDL; importing live ORM metadata into it would make this file silently start
emitting *tomorrow's* schema as `models.py` evolves in later phases, breaking `alembic downgrade
base` / `upgrade head` reproducibility for anyone replaying history. If `models.py` changes, write a
new migration — never edit this one after it has shipped.

Three indexes have no portable `sqlalchemy.Index()` equivalent and are hand-written as raw SQL,
copied verbatim from the PRD where given literally:

1. `vector_outbox (status, id) WHERE status = 'pending'` — a straight partial index, exactly as the
   PRD's "Key DDL details" gives it.
2. `recommendations (user_id) WHERE is_current` — ditto, enforces "exactly one current
   recommendation per user".
3. `events (user_id) INCLUDE (event_type, product_id) WHERE server_ts > now() - INTERVAL '7 days'` —
   the PRD gives this with `now()` in the predicate, but Postgres rejects that outright:
   `functions in index predicate must be marked IMMUTABLE` (verified against a live Postgres 16
   instance while writing this migration; `now()` is STABLE, not IMMUTABLE). The fix used here is
   the standard real-world workaround for a "rolling window" partial index: bake in a literal
   timestamp constant captured at migration-apply time (a literal is trivially immutable) rather
   than a `now()` call. This makes the index correct for every event inserted after the migration
   runs — new rows always satisfy `server_ts > <that past constant>` — but the window stops sliding
   forward from there; the index degrades gracefully into a full (non-partial) index on `user_id`
   over time rather than becoming wrong. Recreating it periodically with a fresh cutoff (e.g. a
   maintenance job, alongside the PRD §6.7 03:00 drift audit) keeps it tight; that's a follow-up, not
   a Phase 1 blocker — see the Phase 1 report-back FOLLOWUPS.

Revision ID: 06a293bc8de0
Revises:
Create Date: 2026-08-09 04:35:49.555399

"""

from datetime import datetime, timedelta, timezone
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "06a293bc8de0"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # citext (case-insensitive email uniqueness) + pgcrypto (gen_random_uuid() for UUID PKs) —
    # per PRD "Key DDL details" / orchestrator notes. Both extensions must exist before any table
    # that uses CITEXT or gen_random_uuid() is created.
    op.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")
    op.execute("CREATE EXTENSION IF NOT EXISTS citext")

    # --- users ---------------------------------------------------------------------------------
    op.execute(
        """
        CREATE TABLE users (
            id            UUID DEFAULT gen_random_uuid() NOT NULL,
            email         CITEXT NOT NULL,
            password_hash TEXT NOT NULL,
            display_name  TEXT NOT NULL,
            role          TEXT DEFAULT 'user' NOT NULL,
            digest_optin  BOOLEAN DEFAULT true NOT NULL,
            created_at    TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
            updated_at    TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
            CONSTRAINT pk_users PRIMARY KEY (id),
            CONSTRAINT ck_users_role_valid CHECK (role IN ('user','admin')),
            CONSTRAINT uq_users_email UNIQUE (email)
        )
        """
    )

    # --- products --------------------------------------------------------------------------------
    op.execute(
        """
        CREATE TABLE products (
            id                UUID DEFAULT gen_random_uuid() NOT NULL,
            title             TEXT NOT NULL,
            description       TEXT DEFAULT '' NOT NULL,
            category          TEXT NOT NULL,
            tags              TEXT[] DEFAULT '{}' NOT NULL,
            level             TEXT NOT NULL,
            price_cents       INTEGER DEFAULT '0' NOT NULL,
            is_active         BOOLEAN DEFAULT true NOT NULL,
            content_hash      TEXT,
            vector_synced_at  TIMESTAMP WITH TIME ZONE,
            created_at        TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
            updated_at        TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
            CONSTRAINT pk_products PRIMARY KEY (id),
            CONSTRAINT ck_products_level_valid
                CHECK (level IN ('beginner','intermediate','advanced')),
            CONSTRAINT ck_products_price_cents_nonnegative CHECK (price_cents >= 0)
        )
        """
    )
    op.execute("CREATE INDEX ix_products_category ON products (category)")
    op.execute("CREATE INDEX ix_products_is_active ON products (is_active)")

    # --- user_profiles ---------------------------------------------------------------------------
    op.execute(
        """
        CREATE TABLE user_profiles (
            user_id            UUID NOT NULL,
            interest_vector    REAL[] DEFAULT '{}' NOT NULL,
            top_categories     JSONB DEFAULT '{}'::jsonb NOT NULL,
            top_terms          JSONB DEFAULT '{}'::jsonb NOT NULL,
            events_since_gen   INTEGER DEFAULT '0' NOT NULL,
            profile_hash       TEXT,
            last_generated_at  TIMESTAMP WITH TIME ZONE,
            created_at         TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
            updated_at         TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
            CONSTRAINT pk_user_profiles PRIMARY KEY (user_id),
            CONSTRAINT fk_user_profiles_user_id_users FOREIGN KEY(user_id)
                REFERENCES users (id) ON DELETE CASCADE
        )
        """
    )

    # --- events (PRD "Key DDL details", reproduced verbatim for the table body) ------------------
    op.execute(
        """
        CREATE TABLE events (
            id            BIGSERIAL NOT NULL,
            user_id       UUID NOT NULL,
            session_id    UUID NOT NULL,
            event_type    TEXT NOT NULL,
            product_id    UUID,
            payload       JSONB DEFAULT '{}'::jsonb NOT NULL,
            weight        REAL DEFAULT '1.0' NOT NULL,
            client_ts     TIMESTAMP WITH TIME ZONE NOT NULL,
            server_ts     TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
            CONSTRAINT pk_events PRIMARY KEY (id),
            CONSTRAINT user_id_session_id_event_type_client_ts
                UNIQUE (user_id, session_id, event_type, client_ts),
            CONSTRAINT fk_events_user_id_users FOREIGN KEY(user_id)
                REFERENCES users (id) ON DELETE CASCADE,
            CONSTRAINT fk_events_product_id_products FOREIGN KEY(product_id)
                REFERENCES products (id) ON DELETE SET NULL
        )
        """
    )
    op.execute("CREATE INDEX ix_events_user_id_server_ts ON events (user_id, server_ts DESC)")
    op.execute("CREATE INDEX ix_events_product_id ON events (product_id)")

    # Rolling-window covering index — see the module docstring for why `now()` can't appear in the
    # predicate directly. `cutoff` is a literal computed once, right now, at migration-apply time.
    cutoff = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
    op.execute(
        f"""
        CREATE INDEX ix_events_user_id_recent ON events (user_id)
            INCLUDE (event_type, product_id)
            WHERE server_ts > TIMESTAMPTZ '{cutoff}'
        """
    )

    # --- vector_outbox (PRD "Key DDL details", reproduced verbatim) -------------------------------
    op.execute(
        """
        CREATE TABLE vector_outbox (
            id             BIGSERIAL NOT NULL,
            product_id     UUID NOT NULL,
            op             TEXT NOT NULL,
            payload        JSONB NOT NULL,
            status         TEXT DEFAULT 'pending' NOT NULL,
            attempts       INTEGER DEFAULT '0' NOT NULL,
            last_error     TEXT,
            created_at     TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
            processed_at   TIMESTAMP WITH TIME ZONE,
            CONSTRAINT pk_vector_outbox PRIMARY KEY (id),
            CONSTRAINT ck_vector_outbox_op_valid CHECK (op IN ('upsert','delete')),
            CONSTRAINT ck_vector_outbox_status_valid CHECK (status IN ('pending','done','failed'))
        )
        """
    )
    op.execute(
        "CREATE INDEX ix_vector_outbox_status_id ON vector_outbox (status, id) "
        "WHERE status = 'pending'"
    )

    # --- agent_runs --------------------------------------------------------------------------------
    op.execute(
        """
        CREATE TABLE agent_runs (
            id                UUID DEFAULT gen_random_uuid() NOT NULL,
            run_id            TEXT NOT NULL,
            user_id           UUID NOT NULL,
            trigger_reason    TEXT NOT NULL,
            node_path         TEXT[] DEFAULT '{}' NOT NULL,
            retrieval_score   REAL,
            refine_loops      INTEGER DEFAULT '0' NOT NULL,
            cache_hit         TEXT,
            models_used       JSONB DEFAULT '{}'::jsonb NOT NULL,
            cost_usd          NUMERIC(10, 6) DEFAULT '0' NOT NULL,
            latency_ms        INTEGER DEFAULT '0' NOT NULL,
            langsmith_url     TEXT,
            status            TEXT DEFAULT 'running' NOT NULL,
            created_at        TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
            completed_at      TIMESTAMP WITH TIME ZONE,
            CONSTRAINT pk_agent_runs PRIMARY KEY (id),
            CONSTRAINT ck_agent_runs_cache_hit_valid
                CHECK (cache_hit IS NULL OR cache_hit IN ('l1','l2')),
            CONSTRAINT ck_agent_runs_status_valid
                CHECK (status IN ('running','success','failed','fallback')),
            CONSTRAINT uq_agent_runs_run_id UNIQUE (run_id),
            CONSTRAINT fk_agent_runs_user_id_users FOREIGN KEY(user_id)
                REFERENCES users (id) ON DELETE CASCADE
        )
        """
    )
    op.execute(
        "CREATE INDEX ix_agent_runs_user_id_created_at ON agent_runs (user_id, created_at)"
    )

    # --- recommendations --------------------------------------------------------------------------
    op.execute(
        """
        CREATE TABLE recommendations (
            id              UUID DEFAULT gen_random_uuid() NOT NULL,
            user_id         UUID NOT NULL,
            agent_run_id    UUID,
            headline        TEXT NOT NULL,
            narrative       TEXT NOT NULL,
            items           JSONB NOT NULL,
            trigger_reason  TEXT NOT NULL,
            is_current      BOOLEAN DEFAULT true NOT NULL,
            created_at      TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
            CONSTRAINT pk_recommendations PRIMARY KEY (id),
            CONSTRAINT fk_recommendations_user_id_users FOREIGN KEY(user_id)
                REFERENCES users (id) ON DELETE CASCADE,
            CONSTRAINT fk_recommendations_agent_run_id_agent_runs FOREIGN KEY(agent_run_id)
                REFERENCES agent_runs (id) ON DELETE SET NULL
        )
        """
    )
    op.execute(
        "CREATE INDEX ix_recommendations_user_id_created_at ON recommendations (user_id, created_at)"
    )
    # Exactly one current recommendation per user (PRD "Key DDL details", reproduced verbatim).
    op.execute(
        "CREATE UNIQUE INDEX ix_recommendations_user_id_is_current ON recommendations (user_id) "
        "WHERE is_current"
    )


def downgrade() -> None:
    # Reverse FK dependency order. Dropping a table drops its own indexes/constraints with it, so
    # no separate DROP INDEX statements are needed. Extensions (pgcrypto, citext) are intentionally
    # left installed — they're schema-agnostic and safe to leave for any other consumer of this
    # database; "downgrade base is clean" is judged on SmartReco tables, not the extension list.
    op.execute("DROP TABLE IF EXISTS recommendations")
    op.execute("DROP TABLE IF EXISTS agent_runs")
    op.execute("DROP TABLE IF EXISTS vector_outbox")
    op.execute("DROP TABLE IF EXISTS events")
    op.execute("DROP TABLE IF EXISTS user_profiles")
    op.execute("DROP TABLE IF EXISTS products")
    op.execute("DROP TABLE IF EXISTS users")
