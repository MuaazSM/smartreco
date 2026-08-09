#!/bin/sh
# SmartReco backend container entrypoint (PRD §13.4: "run migrations on boot or as a release
# command, and seed the catalog once"). POSIX sh — no bashisms — so it runs unmodified on
# python:3.11-slim's default /bin/sh (dash).
#
# `alembic.ini` ships no real `sqlalchemy.url`; app/db/migrations/env.py reads settings.database_url
# (the DATABASE_URL env var) at run time — see that file's comment — so no extra flags are needed
# here beyond the env already being set on the platform.
set -eu

echo "smartreco: running migrations (alembic upgrade head)..."
alembic upgrade head

# One-time catalog seed. OFF by default so a redeploy or restart never wipes a live catalog —
# scripts/seed_data.py resets the products/behavior tables unless run with --no-reset. Set
# SEED_ON_BOOT=true for exactly the first boot after provisioning a fresh DATABASE_URL, confirm the
# seed ran (check logs / GET /api/admin/sync-status), then set it back to false and redeploy.
# Equivalently, skip this flag entirely and run `python scripts/seed_data.py` once as a one-off
# Render shell/job — see README.md §8 step 3 for both options.
if [ "${SEED_ON_BOOT:-false}" = "true" ]; then
    echo "smartreco: SEED_ON_BOOT=true — seeding catalog (one-time)..."
    python scripts/seed_data.py
fi

echo "smartreco: starting uvicorn on port ${PORT:-8000}..."
exec uvicorn app.main:app --host 0.0.0.0 --port "${PORT:-8000}"
