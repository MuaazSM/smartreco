# SmartReco backend image — FastAPI + APScheduler (PRD §13.1/§13.4).
#
# Deployed as a Render Docker web service (see render.yaml, README.md §8). Runs the same code path
# as local dev (`uvicorn app.main:app`); the only difference is migrations run automatically via
# docker-entrypoint.sh instead of a manual `alembic upgrade head`. No application behavior changes —
# this file only packages what already exists.
#
# Single stage: every dependency in requirements.txt ships prebuilt manylinux wheels for
# python:3.11-slim (fastapi, asyncpg, pydantic-core, bcrypt, uvicorn's uvloop/httptools, numpy,
# qdrant-client, langgraph — none need a C/Rust toolchain at install time), so a multi-stage build
# buys nothing here and only adds maintenance surface.
FROM python:3.11-slim

# Unbuffered stdout so logs stream immediately on a host with no persistent shell to `tail -f`;
# no .pyc write-back since the container is rebuilt on every deploy, never mutated in place.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Dependency layer first so it caches independently of app code — most deploys only change app/.
COPY requirements.txt .
RUN pip install -r requirements.txt

# Runtime surface only: application code, Alembic config + migrations (entrypoint runs them on
# boot), one-off ops scripts (seed_data.py, send_digest_now.py, list_free_models.py), the eval
# harness (bonus F9 — runnable via a Render shell against the deployed catalog), and the entrypoint
# itself. Deliberately NOT copied: frontend/ (separate Vercel deploy), docs/, tests/, .env (secrets
# are platform env vars per CLAUDE.md invariant #4, never baked into the image).
COPY app/ app/
COPY alembic.ini .
COPY scripts/ scripts/
COPY evals/ evals/
COPY docker-entrypoint.sh .

# Non-root runtime user (defense in depth; nothing in this app requires root).
RUN chmod +x docker-entrypoint.sh \
    && useradd --create-home --uid 10001 --shell /usr/sbin/nologin smartreco \
    && chown -R smartreco:smartreco /app

USER smartreco

EXPOSE 8000

ENTRYPOINT ["./docker-entrypoint.sh"]
