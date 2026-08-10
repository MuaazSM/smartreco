# SmartReco — Behavioral AI Recommendation Agent

> **Most recommenders guess. SmartReco observes, grades its own retrieval, and refuses to recommend anything it can't point to in the catalog.**

SmartReco is a course-marketplace web app that tracks user behavior, feeds it to a **seven-node LangGraph agent**, retrieves matching products from a vector database via RAG, and generates persuasive, personalized recommendations that **visibly change as behavior changes** — while calling the LLM **fewer than once per 40 events**. Every AI and embedding call routes through a **single Mesh API gateway**.

_Submission for the SmartReco Build Challenge 2026 · Krish Naik Academy × Mesh API._

<!-- TODO(demo): add a 30s GIF of the dashboard changing as a persona's behavior shifts -->

---

## Why this submission is different

A hundred submissions will log a few clicks, stuff them into a prompt, and print the answer. Those fail the same four ways. SmartReco is engineered against each:

| Common failure mode | SmartReco's answer | Where |
|---|---|---|
| Vector DB silently diverges from SQL | **Transactional outbox** — product + `vector_outbox` commit in one transaction; a worker replays to Qdrant with retries. Sync is *provable*. | [`app/services/catalog.py`](app/services/catalog.py), [`app/services/outbox_worker.py`](app/services/outbox_worker.py) |
| LLM invents products not in the catalog | **Grounding validator node** — every `product_id` must be a subset of the retrieved candidates *and* active in Postgres, or the graph loops/falls back. Enforced structurally. | [`app/agent/nodes/validate_grounding.py`](app/agent/nodes/validate_grounding.py) |
| An LLM call per user action | **Interest-drift trigger + 3-layer cache** behind a cooldown and a Redis lock. | [`app/services/trigger.py`](app/services/trigger.py) |
| "Agent" = one `chat.completions.create` | **Seven-node LangGraph** with conditional edges, self-grading, and a bounded refine loop. | [`app/agent/graph.py`](app/agent/graph.py) |

---

## 1. Architecture

```mermaid
flowchart TD
  FE["Next.js frontend<br/>catalog · product · search · dashboard · admin"]
  TR["tracker.ts — Web Worker<br/>ring buffer · throttle · debounce<br/>flush 20 / 10s / sendBeacon"]
  API["FastAPI routes<br/>/auth /products /events /recommendations /admin"]
  ING["Event ingest<br/>202 Accepted → BackgroundTask → bulk insert"]
  TRIG{"Trigger evaluator<br/>drift · threshold · cooldown · lock"}
  AGENT["LangGraph agent — 7 nodes"]
  MESH["Mesh API<br/>per-node model routing"]
  PG[("PostgreSQL<br/>source of truth")]
  OUT["Outbox worker (5s)"]
  QD[("Qdrant<br/>vectors")]
  RD[("Redis<br/>cache · locks")]

  FE --> TR --> API --> ING
  ING -->|after persist| TRIG
  ING -->|write product+outbox one tx| PG
  TRIG -->|only when it matters| AGENT
  AGENT --> MESH
  AGENT -->|store recommendation| PG
  AGENT --> QD
  AGENT --> RD
  PG --> OUT -->|drain| QD
```

- **Backend:** FastAPI (Python 3.11, async throughout), SQLAlchemy 2.0 + Alembic, APScheduler.
- **Data:** PostgreSQL (source of truth), Qdrant (1536-dim cosine vectors), Redis (semantic cache + regeneration lock).
- **AI:** LangGraph agent; **all** LLM + embedding calls through Mesh via [`app/llm/mesh.py`](app/llm/mesh.py).
- **Frontend:** two surfaces on the same API — (1) a **server-rendered Jinja2** frontend served by FastAPI (the suggested stack: [`app/web/`](app/web/) — templates + a batched, non-blocking vanilla-JS tracker), reachable at the backend root (`/`, `/catalog`, `/product/{id}`, `/dashboard`); and (2) a polished **Next.js** App Router app (TypeScript strict, Tailwind; the tracker is a Web Worker) deployed to Vercel. Both drive the same `/api/*` endpoints.

---

## 2. Quickstart (< 5 commands)

```bash
cp .env.example .env            # then set MESH_API_KEY (+ QDRANT_URL/REDIS_URL if not using the compose defaults)
docker compose up -d            # postgres + qdrant + redis
pip install -r requirements.txt
alembic upgrade head && python scripts/seed_data.py   # schema + 40 courses + 1 admin + 3 demo personas
uvicorn app.main:app --reload   # backend on :8000  (frontend: cd frontend && npm install && npm run dev → :3000)
```

**Develop without burning quota:** `MESH_DISABLED=true uvicorn app.main:app` returns recorded fixtures and deterministic offline embeddings — zero AI network calls. The whole test suite runs this way.

Seeded logins (from `scripts/seed_data.py`): `admin@smartreco.dev` (admin) and `ana@ / ben@ / cara@smartreco.dev` (three divergent personas). Log in as each at `/dashboard` to see three visibly different grounded blocks.

---

## 3. Feature checklist & where each lives

**Core (rubric):**
- Auth with two roles (bcrypt-12, JWT HS256, httpOnly cookie) — [`app/core/security.py`](app/core/security.py), [`app/api/routes/auth.py`](app/api/routes/auth.py)
- Clean related schema (Alembic only) — [`app/db/models.py`](app/db/models.py), [`app/db/migrations/`](app/db/migrations/)
- Admin product CRUD + **provable dual-write** (`GET /api/admin/sync-status` → `in_sync: true`) — [`app/api/routes/admin.py`](app/api/routes/admin.py), [`app/services/catalog.py`](app/services/catalog.py), [`app/services/outbox_worker.py`](app/services/outbox_worker.py)
- Non-blocking batched tracking (Web Worker, sendBeacon) — [`frontend/lib/tracker.ts`](frontend/lib/tracker.ts), [`app/api/routes/events.py`](app/api/routes/events.py), [`app/services/event_ingest.py`](app/services/event_ingest.py)
- Incremental interest profile (decayed centroid, 3-day half-life) — [`app/services/profile.py`](app/services/profile.py)
- RAG agent that consumes behavior and reasons, grounded — [`app/agent/`](app/agent/), [`app/vector/hybrid_retriever.py`](app/vector/hybrid_retriever.py)
- Trigger + 3-layer cache (don't call the LLM every action) — [`app/services/trigger.py`](app/services/trigger.py)
- Dashboard that visibly changes — [`frontend/app/dashboard/`](frontend/app/dashboard/)

**Bonuses shipped:**
- ⭐ **Structured agent framework** — 7-node LangGraph, conditional edges, bounded refine loop → [`app/agent/graph.py`](app/agent/graph.py)
- ⭐ **Retrieval polish** — dense + BM25 → Reciprocal Rank Fusion → metadata filter → MMR (λ=0.7) → top-12 → [`app/vector/hybrid_retriever.py`](app/vector/hybrid_retriever.py); a functional pairwise reranker (Mesh cheap-tier, **permutation-only so grounding is preserved**, opt-in via `run_agent(enable_rerank=True)`, with a structural lift-measurement harness) at [`app/vector/reranker.py`](app/vector/reranker.py)
- ⭐ **Observability** — `GET /api/admin/agent-runs` (node path, retrieval score, refine loops, tokens, cost, latency) + `GET /api/admin/metrics` → [`app/api/routes/admin.py`](app/api/routes/admin.py), [`app/services/metrics.py`](app/services/metrics.py); LangSmith is env-ready (set `LANGSMITH_*`)
- ⭐ **Scheduled proactive delivery (F7)** — APScheduler 09:00 digest + 03:00 drift audit on the same scheduler as the 5s drain, Jinja2 recap-with-hook email (graceful save-to-disk without SMTP), token-protected `POST /api/internal/run-digest`, and a GitHub Actions cron for the sleeping-host trap → [`app/scheduler/jobs.py`](app/scheduler/jobs.py), [`app/scheduler/digest.py`](app/scheduler/digest.py), [`app/api/routes/internal.py`](app/api/routes/internal.py), [`.github/workflows/digest.yml`](.github/workflows/digest.yml); demo it with `python scripts/send_digest_now.py`
- **Evaluation harness** — 10 synthetic profiles scored on groundedness / behavioral relevance / persuasion / diversity → [`evals/eval_recommendations.py`](evals/eval_recommendations.py), results in [`evals/results.md`](evals/results.md)

**See it change (the 60-second demo):** with the backend running and the catalog seeded, [`python scripts/simulate_behavior.py`](scripts/simulate_behavior.py) replays three personas' behavior against the live API and prints each dashboard block evolving as their interests shift — the fastest way to watch the whole system work end to end with no manual clicking.

---

## 4. Efficiency — measured, not claimed

| Metric | Result | How measured |
|---|---:|---|
| LLM generations / 100 events | **~2** (target < 2.5) | offline replay through the real ingest → profile → trigger pipeline; cooldown + L1/L2 suppress most batches |
| Reduction vs LLM-per-event | **~20–50×** | vs the naive 100 generations / 100 events |
| Tracker p95 client handler time | **0.0018 ms**, 0 dropped / 1000 | [`tests/test_tracker_load.py`](tests/test_tracker_load.py) |
| Dual-write sync | **40 active == 40 Qdrant points**, `in_sync: true` | live check against the configured Qdrant |
| Test suite | **57 passing** | `pytest -q` under `MESH_DISABLED=true` |

The trigger policy — not the model choice — is the real cost lever: `(events_since_gen ≥ 8 OR drift > 0.15 OR high-intent) AND cooldown (>10 min) AND Redis lock`, then L1 exact cache (`profile_hash`), then L2 semantic cache (cosine > 0.95, one cheap re-personalization), then a full run.

---

## 5. Design decisions & trade-offs

- **Transactional outbox over dual-write-in-handler.** A request never touches Qdrant; the product row and an outbox row commit together, and a 5s worker (`SELECT … FOR UPDATE SKIP LOCKED`) drains it with retries and content-hash re-embed skipping. Sync becomes an auditable claim, not a hope. Deactivating a product (via `DELETE` *or* `PATCH is_active=false`) enqueues a `delete` so a point is never orphaned.
- **Per-node model routing through Mesh.** Cheap free models for `plan_queries`/`grade_retrieval`; a quality model only for `generate`. Swapping models is a one-line change because Mesh is a single gateway — see the routing table in [`app/llm/model_router.py`](app/llm/model_router.py).
- **Postgres + Qdrant.** Postgres for JSONB event payloads, partial indexes, and concurrent writes; Qdrant for first-class payload filtering and hybrid search. One `docker-compose.yml` brings both up.
- **`MESH_DISABLED=true` fixtures.** The graph, frontend, and full test suite build and run with zero AI network calls and deterministic embeddings — offline dev and CI cost nothing.

**Non-goals (explicit):** payments/checkout, real course content, OAuth/social login, multi-tenancy, horizontal scaling/Kubernetes, an A/B testing harness (we describe the hook, we don't build it).

---

## 6. Mesh API compliance

Every LLM and embedding call is constructed in exactly one place — [`app/llm/mesh.py`](app/llm/mesh.py):

```python
client = AsyncOpenAI(base_url="https://api.meshapi.ai/v1", api_key=settings.MESH_API_KEY)
```

No other AI-provider SDK is imported or listed in `requirements.txt`. This is enforced by an AST-based import-lint, [`tests/test_single_gateway.py`](tests/test_single_gateway.py), which asserts exactly one client construction site and no banned provider SDK. Free-tier models are resolved at startup from Mesh's `/v1/models` with a preference chain and a paid fallback ([`app/llm/model_router.py`](app/llm/model_router.py)); `python scripts/list_free_models.py` prints the resolved catalog.

---

## 7. Evaluation results

Full run in [`evals/results.md`](evals/results.md) (10 personas through the real seven-node graph):

| Metric | Target | Measured (offline) | Notes |
|---|---|---:|---|
| Groundedness | 100% | **100.0%** | structural — fully meaningful in any mode |
| Diversity | ≥ 2 | **2.80** | unique categories per rec set |
| Behavioral relevance | ≥ 0.70 | 0.30 † | needs live embeddings (see below) |
| Persuasion (LLM-judge) | ≥ 4.0 | 4.28 † | fixture-derived offline; needs live Mesh |

† Behavioral relevance and persuasion require **live Mesh**: this environment's catalog was seeded under `MESH_DISABLED=true`, so Qdrant holds deterministic *pseudo-random* vectors (no semantic signal), and the `generate` narrative is a fixture. Groundedness (100%) and diversity (2.80) are real. Re-seed with live embeddings and rerun the harness for true numbers — no code change needed; the harness auto-detects the mode.

---

## 8. Deployment & known limits

The whole stack fits inside free tiers (PRD §13.1). **Recommended topology** — same-origin via a
Vercel rewrite, so the httpOnly auth cookie stays `SameSite=Lax` with **no CORS at all**:

| Component | Host | Why |
|---|---|---|
| Next.js frontend | **Vercel** | zero-config Next.js detection; [`frontend/next.config.mjs`](frontend/next.config.mjs) already rewrites `/api/:path*` and `/health` to `BACKEND_ORIGIN` |
| FastAPI backend + scheduler | **Render**, Docker web service | [`Dockerfile`](Dockerfile) + [`render.yaml`](render.yaml); the 5s outbox drain needs a continuous process, which a Docker web service provides |
| PostgreSQL | **Render** (managed) | provisioned by [`render.yaml`](render.yaml) and auto-wired via `fromDatabase`; free tier expires ~30 days |
| Qdrant | **Qdrant Cloud** | 1 GB free is far more than a 40-item catalog needs |
| Redis | **Render Key Value** | provisioned by [`render.yaml`](render.yaml) and auto-wired via `fromService`; L2 cache + regeneration lock |

The browser only ever talks to the Vercel origin. Vercel's rewrite proxies `/api/*` server-to-server
to Render — that hop is not a browser-initiated cross-site request, so the cookie set on a
Vercel-origin response round-trips normally and `tracker.ts`'s `sendBeacon` keeps carrying it. This
is the PRD §13.2 same-origin resolution, achieved without literally colocating the two processes.

### Deploy steps

1. **Provision Qdrant Cloud** (the only external data service — Postgres and Redis are provisioned by
   the blueprint) and have the Mesh key ready.
   - Qdrant Cloud → cluster URL + API key → `QDRANT_URL` / `QDRANT_API_KEY`.
   - Mesh dashboard → `MESH_API_KEY` (`rsk_...`).

2. **Deploy the backend to Render**: dashboard → New → Blueprint → point at this repo. Render reads
   [`render.yaml`](render.yaml), which provisions a free **Postgres** and **Key Value (Redis)** and
   builds [`Dockerfile`](Dockerfile) as a Docker web service with `healthCheckPath: /health`.
   `DATABASE_URL` and `REDIS_URL` are wired automatically via `fromDatabase`/`fromService`; you only
   paste the `sync: false` vars — `QDRANT_URL`, `QDRANT_API_KEY`, `MESH_API_KEY`, and (once Vercel is
   up) `FRONTEND_URL`. `JWT_SECRET` and `DIGEST_TRIGGER_TOKEN` are auto-generated by the blueprint.
   **Migrations run automatically on every boot** — [`docker-entrypoint.sh`](docker-entrypoint.sh)
   runs `alembic upgrade head` before starting uvicorn, so there is no separate release-command step
   to configure.

3. **Seed the catalog once.** A deployed instance with an empty catalog "correctly retrieves
   nothing," which looks like a broken agent (PRD §13.4). Either:
   - run `python scripts/seed_data.py` once as a one-off Render Shell/job against the now-migrated
     `DATABASE_URL`, **or**
   - set `SEED_ON_BOOT=true` for exactly the first deploy (the entrypoint runs the seed after
     migrations), confirm `GET /api/admin/sync-status` returns `in_sync: true`, then set it back to
     `false` and redeploy. `seed_data.py` resets the catalog by default on every run, so leaving this
     flag on would wipe real data on every restart.

4. **Deploy the frontend to Vercel**: import this repo with root directory `frontend/` (Next.js
   auto-detected). Set **`BACKEND_ORIGIN`** to the Render backend's URL (e.g.
   `https://smartreco-backend.onrender.com`) — the one env var
   [`next.config.mjs`](frontend/next.config.mjs)'s rewrite needs. No `vercel.json` is required;
   Vercel honors the Next.js rewrite config with zero extra configuration.

5. **The proxy/cookie explanation, and the alternative (PRD §13.2).** With the rewrite in place,
   `SameSite=Lax` (the default, `COOKIE_SAMESITE=lax`) keeps working exactly as it does locally — no
   CORS middleware exercise needed. If you instead split the two — the frontend calling the Render
   URL directly, bypassing the rewrite — the cookie stops surviving, since Lax does not cross sites.
   For that path: set `COOKIE_SAMESITE=none` on the backend (the cookie also needs `Secure`, implied
   by HTTPS on both platforms), enable `CORSMiddleware` with `allow_credentials=True` and
   `CORS_ORIGINS` set to the **exact** Vercel URL (a wildcard origin is rejected by browsers once
   credentials are enabled), and confirm `navigator.sendBeacon` in `tracker.ts` still fires with
   credentials — beacons carry cookies only under these conditions. `app/core/config.py` and
   `app/main.py`'s `CORSMiddleware` already support this path; it just isn't the primary
   recommendation. **Test whichever mode you pick on the deployed URL, not localhost** — every
   same-origin assumption holds locally and the bug is invisible there.

6. **Wire the GitHub Actions digest cron (PRD §13.3 — the scheduler trap).** Free hosts sleep on
   idle, so the in-process 09:00 APScheduler job never fires once deployed.
   [`.github/workflows/digest.yml`](.github/workflows/digest.yml) already calls
   `POST /api/internal/run-digest` on a daily cron; set, under this repo's Settings → Secrets and
   variables → Actions:
   - secret **`DIGEST_TRIGGER_TOKEN`** — must match the value Render generated for the backend's
     `DIGEST_TRIGGER_TOKEN` env var.
   - variable **`DIGEST_API_BASE_URL`** — the Render backend URL, no trailing slash.

   The **5s outbox drain stays in-process regardless** (`SMARTRECO_RUN_SCHEDULER=true` on Render) —
   it must be continuous, which a Docker web service satisfies unlike a sleeping/serverless host.

7. **Pre-demo checklist (PRD §13.5) — run every item against the deployed URL, not locally:**
   1. `GET /health` reports `db`, `qdrant`, `redis` and `mesh` all reachable.
   2. Register a fresh user, log in, confirm the cookie is set and survives a reload.
   3. Browse three courses; confirm events land (`SELECT count(*) FROM events`).
   4. `GET /api/admin/sync-status` returns `in_sync: true`.
   5. Trigger a recommendation; confirm it is grounded and `agent_runs` has a row with a full node
      path.
   6. Fire `scripts/send_digest_now.py` (or the digest workflow's manual "Run workflow" button);
      confirm the email arrives.
   7. Pre-generate recommendations for the three demo personas so the recording never waits on a
      cold LLM call.

### Env vars a deployer must set

Names match `app/core/config.py`'s `Settings` fields exactly (uppercased); see
[`.env.example`](.env.example) for the local-dev defaults these override. `render.yaml` ships every
one of these as a documented placeholder.

**Backend (Render):**

| Var | Source | Notes |
|---|---|---|
| `DATABASE_URL` | Render (auto-wired) | `fromDatabase`; `postgresql://…` is rewritten to the asyncpg driver in `config.py` |
| `QDRANT_URL` / `QDRANT_API_KEY` | Qdrant Cloud | paste in the dashboard |
| `REDIS_URL` | Render Key Value (auto-wired) | `fromService` |
| `MESH_CHEAP_MODEL` / `MESH_QUALITY_MODEL` | fixed in blueprint | `openai/gpt-4o-mini` / `openai/gpt-4.1-mini` — fast models so runs finish in budget |
| `AGENT_TIMEOUT_SECONDS` | fixed in blueprint | `60` — headroom over Mesh's gateway latency |
| `MESH_API_KEY` | Mesh dashboard | `rsk_...` — never commit |
| `MESH_BASE_URL` | fixed | `https://api.meshapi.ai/v1` |
| `MESH_DISABLED` | fixed | `false` in production |
| `JWT_SECRET` | Render `generateValue` | rotate; never reuse the local dev value |
| `COOKIE_SAMESITE` | you set | `lax` (recommended topology) or `none` (split + CORS) |
| `CORS_ORIGINS` | you set | unused under the rewrite topology; the exact Vercel URL under split + CORS |
| `FRONTEND_URL` | you set | the Vercel URL — used for links in digest emails |
| `SMTP_HOST` / `SMTP_PORT` / `SMTP_USER` / `SMTP_PASSWORD` / `SMTP_FROM` or `RESEND_API_KEY` | optional | digest bonus F7; saves to disk/logs if neither is set |
| `LANGSMITH_API_KEY` / `LANGSMITH_PROJECT` / `LANGSMITH_TRACING` | optional | bonus F8 |
| `DIGEST_TRIGGER_TOKEN` | Render `generateValue` | copy into the GitHub Actions secret of the same name |
| `SMARTRECO_RUN_SCHEDULER` | fixed | `true` — keeps the 5s outbox drain running (read via `os.getenv` in `app/main.py`, not a `Settings` field) |
| `SEED_ON_BOOT` | you set | `false` normally; `true` only for the first boot — entrypoint-only shell flag, not a `Settings` field |

**Frontend (Vercel):**

| Var | Notes |
|---|---|
| `BACKEND_ORIGIN` | the Render backend URL; consumed by `frontend/next.config.mjs`'s rewrite |

### Build/run artifacts in this repo

[`Dockerfile`](Dockerfile) (single-stage, `python:3.11-slim`, non-root user, `EXPOSE 8000`),
[`docker-entrypoint.sh`](docker-entrypoint.sh) (`alembic upgrade head` → optional gated seed →
`exec uvicorn`), [`.dockerignore`](.dockerignore), and [`render.yaml`](render.yaml) (blueprint with
every env var above as a placeholder). Verified locally: `docker build -t smartreco-backend -f
Dockerfile .` succeeds, and running the built image against the local `docker-compose` Postgres/
Qdrant/Redis with `MESH_DISABLED=true` runs migrations, boots uvicorn, and returns
`{"status":"ok", ...}` from `/health`.

- **CI:** `.github/workflows/smartreco-checks.yml` is the official hackathon eligibility workflow
  (separate from the digest workflow above). It needs repo secrets `MESH_API_KEY` and
  `SUBMISSION_TOKEN`, and the entry form submitted on the dashboard for the result to record. Local
  critical checks (compile + deps + Mesh key valid) pass.
- **Live-Mesh caveats (important for a live demo):** a live run needs a funded Mesh balance **or**
  free chat + embedding models available on the account — `text-embedding-3-small` is a paid model,
  and a balance-less key returns `402 spend_limit_exceeded`. Free-model resolution was hardened to
  tolerate Mesh's bare-list `/v1/models` response ([`app/llm/model_router.py`](app/llm/model_router.py)).
  Everything runs fully offline under `MESH_DISABLED=true`; re-seed with live embeddings and rerun
  the eval harness for true relevance/persuasion numbers once Mesh is live (§7).

**Frontend note (compliance):** the brief requires a Python backend and only *suggests* Jinja2. Next.js was chosen for the tracker's reliability (Web Worker + `sendBeacon`); the backend is FastAPI per the requirement, and `requirements.txt` stays at the repo root where the checker looks.

---

## 9. Repository map

```
app/  main.py · core/ · db/ · api/routes/ · services/ · vector/ · agent/ · llm/ · scheduler/
frontend/  Next.js App Router; lib/tracker.ts is the performance-critical file
scripts/  seed_data.py · list_free_models.py
evals/  eval_recommendations.py · dataset.json · results.md
tests/  test_single_gateway · test_dual_write · test_grounding · test_trigger (the four invariant tests) + more
```

Run the guardrails locally before pushing: `python -m compileall app scripts evals tests -q && pytest -q`.
