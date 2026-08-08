# SmartReco — Implementation Plan

A phase-by-phase build plan derived from `SmartReco-PRD.pdf` (§ references point at PRD sections)
and the invariants in `CLAUDE.md`. Phases are ordered by **dependency**, not by PRD day, but each
maps to a delivery-plan day (§11) for scheduling. Build top-to-bottom; **cut from the bottom** if time
runs short (see [Cut order](#cut-order)).

> **Timeline reality.** PRD §11 budgets Day 1 = Aug 7–8, Day 2 = Aug 9, Day 3 = Aug 10, Day 4 = Aug 11.
> The repo currently holds only `CLAUDE.md` and the PRD. Phases 0–4 (Day-1 work) are the immediate
> critical path and must land before anything else can be demonstrated.

---

## Guiding principles (apply to every phase)

These are the [hard invariants](CLAUDE.md) — a change that breaks one is submission-invalidating. Re-read
before each phase.

1. **Single AI gateway.** Every LLM *and* embedding call goes through `app/llm/mesh.py` → Mesh API. No
   second provider SDK in `requirements.txt` or imported anywhere. Enforced by `tests/test_single_gateway.py`.
2. **Grounded only.** Recommendations reference real catalog rows; `validate_grounding` rejects any
   `product_id` not in the retrieved candidate set. No "return what the LLM said" fallback.
3. **No Qdrant writes in a request handler.** Product row + `vector_outbox` row commit in one transaction;
   the worker drains the outbox.
4. **No secrets in the repo.** `.env` gitignored; `.env.example` holds placeholders only.
5. **All Python compiles.** `python -m compileall app scripts evals tests -q` is a CI gate.
6. **No LLM call per event.** Regeneration passes the trigger policy in `app/services/trigger.py`.

### Cross-cutting concerns (wire once, reuse everywhere)

| Concern | Mechanism | Introduced in |
|---|---|---|
| Offline dev / quota safety | `MESH_DISABLED=true` returns fixtures for every node & embeddings | Phase 2 |
| Structured logging | JSON logs via `app/core/logging.py`; every agent run carries a `run_id` through API → node → Mesh log | Phase 0 |
| Schema changes | Alembic migration for **every** change; `server_default` for timestamps; index anything filtered/sorted | Phase 1 |
| Verification gate | `compileall` + `pytest -q` green before a phase is "done" | Phase 0 |
| Config | `pydantic-settings` in `app/core/config.py`; nothing reads `os.environ` directly | Phase 0 |

### Definition of done (per component)

A component is done when it has: an Alembic migration if it touched the schema · a test covering its
invariant · no hardcoded secrets · `compileall` passing · a README line if it's a judged feature (bonus
items must link to the exact implementing file).

---

## Phase map

| Phase | Deliverable | PRD refs | Depends on | PRD day | Cut? |
|---|---|---|---|---|---|
| 0 | Scaffolding, infra, config, CI | §4, §9, §13.4 | — | Day 1 | Never |
| 1 | Data model + Alembic migrations | §5 | 0 | Day 1 | Never |
| 2 | Mesh gateway (client + model router) | §6.5, §7 | 0 | Day 1 | Never |
| 3 | Authentication (F1) | §6.1 | 1 | Day 1 | Never |
| 4 | **Catalog + transactional outbox dual-write (F2)** | §6.2 | 1, 2, 3 | Day 1 | **Never** |
| 5 | Behavioral tracking + profile builder (F3) | §6.3 | 1, 4 | Day 2 | Never |
| 6 | **LangGraph agent + retrieval + grounding (F4)** | §6.4 | 2, 4, 5 | Day 3 | **Never** |
| 7 | Trigger policy + three-layer cache (F5) | §6.5 | 5, 6 | Day 3 | Core |
| 8 | Dashboard + surfacing (F6) | §6.6 | 5, 6, 7 | Day 3 | Core |
| 9 | Bonuses: digest, observability, evals, rerank | §6.7, §6.8, §10 | 6, 7 | Day 4 AM | Bonus |
| 10 | README, deployment, demo, final push | §13, §14, §15 | all | Day 4 PM | Core |

**Never cut:** dual-write integrity (P4), the LangGraph loop (P6), grounding validation (P6), Mesh routing (P2).

### Dependency graph

```
0 ──► 1 ──► 3 ──┐
 └──► 2 ────────┼──► 4 ──► 5 ──► 6 ──► 7 ──► 8 ──► 10
                                  └────────────► 9 ─┘
```

---

## Phase 0 — Scaffolding, infrastructure & CI

**Goal:** a one-command local stack, a compiling skeleton, and a green CI pipeline before any feature code.
**PRD:** §4 (architecture), §9 (repo structure), §13.4 (config & secrets).

### Tasks
- [ ] Create the `app/ scripts/ evals/ tests/` tree exactly as PRD §9. Keep all Python inside these four
      roots — stray `.py` in `frontend/` trips the CI compile check.
- [ ] `docker-compose.yml` — postgres, qdrant, redis; healthchecks; named volumes. `docker compose up -d`
      must be the *only* setup command.
- [ ] `requirements.txt` at repo root (where the checker looks) — see dependency table below. **Must list a
      web framework (`fastapi`) and an LLM client (`openai`)** — this is a critical CI check (§2).
- [ ] `.env.example` (committed, placeholders) + `.gitignore` (**must** contain `.env`, `node_modules/`,
      `*.db`, `__pycache__/`, Qdrant storage dirs).
- [ ] `app/core/config.py` — `pydantic-settings` `Settings` reading every env var from §13.4.
- [ ] `app/core/logging.py` — structured JSON logger; `run_id` context helper.
- [ ] `app/core/security.py` — password hashing + JWT helpers (bodies filled in Phase 3).
- [ ] `app/main.py` — FastAPI app + `lifespan` (starts APScheduler in later phases), `GET /health`
      (db · qdrant · redis · mesh reachability — §8).
- [ ] `.github/workflows/smartreco-checks.yml` — `python -m compileall app scripts evals tests -q` **and**
      `pytest -q` on every push. Green CI is an eligibility requirement.
- [ ] Fix the doc-path drift: `CLAUDE.md` references `docs/SmartReco-PRD.md`; either add that path or update
      the reference so a judge/tool finds the spec.

### Recommended dependencies (one-sentence justification each — per CLAUDE.md)

| Package | Why |
|---|---|
| `fastapi`, `uvicorn[standard]` | required web framework (critical CI check) |
| `openai` | the only LLM client SDK; points at Mesh base URL |
| `sqlalchemy[asyncio]`, `asyncpg`, `alembic` | async ORM + migrations over Postgres |
| `pydantic`, `pydantic-settings` | request/response + structured-output schemas + config |
| `langgraph`, `langchain-core` | the seven-node agent graph and checkpointer |
| `qdrant-client` | vector store client (behind an interface) |
| `redis` | semantic cache + distributed regeneration lock |
| `tenacity` | exponential backoff on Mesh 429/5xx |
| `apscheduler` | 5s outbox drain + local digest/drift jobs |
| `passlib[bcrypt]`, `pyjwt` | bcrypt cost-12 hashing + HS256 JWT |
| `rank-bm25` | lexical BM25 leg of hybrid retrieval — **not an AI model**, does not violate invariant #7 |
| `numpy` | MMR / decayed-centroid vector math |
| `jinja2` | digest email templates (bonus) |
| `python-multipart` | form parsing for auth |
| `pytest`, `pytest-asyncio` | async test suite |

### Definition of done
`docker compose up -d` brings up all three services healthy · `uvicorn app.main:app` boots · `GET /health`
returns component status · `compileall` + an empty `pytest` pass in CI.

---

## Phase 1 — Data model & migrations

**Goal:** the full schema from §5, created only through Alembic (no `create_all()` in app code).
**PRD:** §5 + "Key DDL details".

### Tasks
- [ ] `app/db/models.py` — SQLAlchemy models for: `users`, `user_profiles`, `products`, `events`,
      `vector_outbox`, `agent_runs`, `recommendations`.
- [ ] `app/db/session.py` — async engine + `AsyncSession` factory + FastAPI dependency.
- [ ] `alembic/` init + **initial migration** capturing every constraint below.
- [ ] Qdrant collection bootstrap helper: collection `products`, **cosine** distance, 1536-dim, payload
      `{product_id, title, category, tags, level, price_cents, is_active}`, **point ID = product_id** (so
      upsert is idempotent). Create on startup if absent.

### Schema specifics that must not be dropped
- `events`: `UNIQUE (user_id, session_id, event_type, client_ts)` → retries are idempotent (do **not**
  remove). `weight REAL NOT NULL DEFAULT 1.0` is **server-assigned**. Indexes: `(user_id, server_ts DESC)`
  and a partial `INCLUDE (event_type, product_id) WHERE server_ts > now() - INTERVAL '7 days'`.
- `vector_outbox`: `op IN ('upsert','delete')`, `status IN ('pending','done','failed')`, `attempts`,
  `last_error`, `processed_at`; partial index `WHERE status = 'pending'`.
- `recommendations`: `UNIQUE ... WHERE is_current` → exactly one current recommendation per user.
- `products`: `content_hash` (skip re-embed if unchanged) + `vector_synced_at`.
- `user_profiles`: `interest_vector real[]`, `top_categories jsonb`, `top_terms jsonb`,
  `events_since_gen int`, `profile_hash` (the L1 cache key), `last_generated_at`.
- `agent_runs`: `node_path text[]`, `retrieval_score`, `refine_loops`, `cache_hit`, `models_used jsonb`,
  `cost_usd`, `latency_ms`, `langsmith_url`, `status`.
- Timestamps use `server_default=now()`, not Python defaults.

### Definition of done
`alembic upgrade head` builds every table + index · `alembic downgrade base` is clean · Qdrant `products`
collection is created idempotently · models import without error under `compileall`.

---

## Phase 2 — Mesh gateway (the single AI client)

**Goal:** one place that constructs an LLM/embedding client, with per-node model routing and quota safety.
**PRD:** §7 (Mesh integration), §6.5 (per-node routing), §6.5.1 (near-zero cost).
**Invariant:** #1 and #7.

### Tasks
- [ ] `app/llm/mesh.py` — **the only** `AsyncOpenAI(base_url="https://api.meshapi.ai/v1",
      api_key=settings.MESH_API_KEY)` in the codebase. Wrapper responsibilities:
  - per-node model resolution via `model_router.resolve(node, tier)` — never hardcode a model ID at a call site;
  - `tenacity` exponential backoff on 429/5xx; **20s timeout**;
  - token + cost accounting written to `agent_runs`;
  - Pydantic-validated JSON mode for structured outputs;
  - embeddings via `/v1/embeddings` (`openai/text-embedding-3-small`, 1536-dim) with a `content_hash` cache;
  - **`MESH_DISABLED=true`** short-circuit returning recorded fixtures for every node and for embeddings.
- [ ] `app/llm/model_router.py` — `FREE_PREFERENCE` list + `PAID_FALLBACK = {"cheap": "openai/gpt-4o-mini",
      "quality": "anthropic/claude-sonnet-4.5"}`. `free_model_ids()` reads `client.models.list()` `is_free`
      flag at startup (`lru_cache`); `resolve()` walks the preference chain, falls back to paid. Routing:
      `plan_queries`/`grade_retrieval`/`rerank` → free instruct (cheap); `generate` → free large → quality;
      embeddings → cheap.
- [ ] `scripts/list_free_models.py` — 3-line wrapper over `client.models.list()`; its output is pasted into
      the README to prove routing is real.
- [ ] `tests/test_single_gateway.py` — **import-lint**: assert exactly one client construction site and no
      banned provider SDK (`groq`, `openrouter`, `anthropic`, `google-generativeai`, `together`,
      `sentence-transformers`, `fastembed`) in imports or `requirements.txt`. **Never weaken or skip this.**

### Definition of done
All model choice flows through `resolve()` · `MESH_DISABLED=true` runs the whole app with zero network AI
calls · `list_free_models.py` prints the resolved free catalog · `test_single_gateway.py` green.

---

## Phase 3 — Authentication (F1)

**Goal:** email/password auth with roles; simple by design (no OAuth).
**PRD:** §6.1, §8 (auth routes). **Non-goal:** social login.

### Tasks
- [ ] `app/core/security.py` — bcrypt **cost 12**; JWT **HS256, 24h**, `role` claim; encode/decode helpers.
- [ ] `app/api/routes/auth.py` — `POST /api/auth/register` (email, password, display_name),
      `POST /api/auth/login` (sets httpOnly JWT cookie), `GET /api/auth/me`.
- [ ] Cookie: httpOnly, `SameSite` from config (`lax` local / `none;secure` cross-origin — §13.2).
- [ ] `require_role("admin")` FastAPI dependency guarding all admin routes.
- [ ] `tests/test_auth.py` — register→login→me round-trip; admin guard rejects non-admin; password never
      returned; token carries role.

### Definition of done
Register/login/me work · JWT in httpOnly cookie with role claim · admin dependency blocks non-admins ·
tests green.

---

## Phase 4 — Catalog + transactional outbox dual-write (F2) ★ core differentiator

**Goal:** product CRUD that keeps Postgres and Qdrant provably in sync via a committed outbox. **Never call
Qdrant inside a request handler.**
**PRD:** §6.2 (+ Figure 3). **Invariant:** #3.

### Tasks
- [ ] `app/services/catalog.py` — create/update/delete product **and** insert the matching `vector_outbox`
      row (`op='upsert'|'delete'`) in **one transaction**. Update recomputes `content_hash`. Delete is a
      soft-delete (`is_active=false`) plus an outbox `delete` op. Returns fast — **no vector call in the path**.
- [ ] `app/vector/qdrant_client.py` — thin wrapper behind an interface (Chroma is the documented fallback).
- [ ] `app/vector/embeddings.py` — Mesh `/v1/embeddings` + `content_hash` cache (skip re-embed when unchanged).
- [ ] `app/services/outbox_worker.py` — drain loop **every 5s**: `SELECT ... FOR UPDATE SKIP LOCKED` claims
      pending rows (concurrent-worker safe); skip re-embed on unchanged `content_hash`; upsert/delete the
      Qdrant point (id = product_id); on success set `status='done'`, `products.vector_synced_at=now()`; on
      failure `attempts++`, exponential backoff, surface to admin console after 5 attempts (`status='failed'`).
- [ ] Wire the 5s drain into APScheduler in `app/main.py` lifespan (this drain must be **in-process and
      continuous** even in deployment).
- [ ] `app/api/routes/admin.py` — `POST/PATCH/DELETE /api/admin/products` (admin-guarded);
      `GET /api/admin/sync-status` comparing Postgres active-product IDs vs Qdrant point IDs, returning
      `{in_sync, missing_in_vector, orphaned_in_vector, outbox_lag_seconds}`. **Healthy system → `in_sync: true`.**
- [ ] `app/api/routes/products.py` — `GET /api/products?category&level&q&page`, `GET /api/products/{id}`.
- [ ] `scripts/seed_data.py` — 40 courses + 1 admin + **3 demo users with pre-baked divergent behavior
      histories** (makes the demo instantly convincing).
- [ ] `tests/test_dual_write.py` — product create writes both a product row and a pending outbox row in one
      tx; worker drains it; `sync-status` returns `in_sync: true`; unchanged update skips re-embedding;
      delete removes the Qdrant point. **Invariant test — keep strong.**

### Gotchas
Treat a `sync-status` failure as **P0** — it's the single most demonstrable claim in the submission. The
transaction boundary (product + outbox commit together or not at all) *is* the point.

### Definition of done
Admin CRUD lands in Postgres and, after ≤1 drain cycle, in Qdrant · `sync-status` → `in_sync: true` ·
re-embed skipped when content unchanged · `test_dual_write.py` green · seed script populates 40 courses +
4 users.

---

## Phase 5 — Behavioral tracking + profile builder (F3)

**Goal:** provably non-blocking, batched, lossless tracking → server-weighted events → incremental profile.
**PRD:** §6.3 (+ Figure 4). **Invariant:** #6 adjacent (ingest path must never call the LLM).

### Backend tasks
- [ ] `app/api/routes/events.py` — `POST /api/events/batch` → Pydantic validate → **`202 Accepted`** →
      `BackgroundTask` single bulk `COPY`/insert.
- [ ] `app/services/event_ingest.py` — bulk insert; **server-side weighting** (view 1.0 · click 1.5 ·
      search 2.0 · dwell>30s 2.5 · cart 3.0) — never trust a client weight. Natural key dedupes retries.
- [ ] `app/services/profile.py` — incremental profile update: **decayed interest centroid (3-day half-life)**,
      `top_categories`, `top_terms`, level affinity, recompute `profile_hash`, bump `events_since_gen`. This
      runs after ingest; it does **not** call the LLM.

### Frontend tasks (Next.js App Router — begins here)
- [ ] Scaffold `frontend/` (Next.js + TypeScript strict + Tailwind). Server components by default.
- [ ] `frontend/lib/tracker.ts` — **the performance-critical file**:
  - ring buffer **cap 200, drop-oldest**, inside a **Web Worker** (never block main thread, no sync XHR);
  - flush at **20 events / 10s / whichever first**, plus `visibilitychange → hidden` via
    `navigator.sendBeacon` (survives tab close);
  - scroll **throttled 1/500ms** reduced to milestone depths 25/50/75/100; search **debounced 400ms**;
    dwell via `IntersectionObserver`, foreground time only, one event on unmount;
  - failed batches → `localStorage` spillover, replayed next load; client-generated event UUID for idempotency.
- [ ] `tests/test_tracker_load.py` — Playwright/k6 script firing **1,000 events**, asserting p95 client-side
      handler time **< 2 ms** with **zero dropped events**. One chart in the README.

### Definition of done
Events batch to the server, land with correct server weights, and dedupe on retry · profile updates
incrementally with a decayed centroid · tracker never blocks the main thread and survives tab close ·
1,000-event load test passes.

---

## Phase 6 — LangGraph agent + retrieval + grounding (F4) ★ centerpiece

**Goal:** a real seven-node graph with conditional edges, self-grading, a bounded refine loop, and a
structural grounding gate. **The system never shows an ungrounded recommendation.**
**PRD:** §6.4 (+ Figure 5). **Invariant:** #2.

### Retrieval tasks
- [ ] `app/vector/hybrid_retriever.py` — per query: **dense vector search + BM25**, fused with **Reciprocal
      Rank Fusion**, metadata-filtered, then **MMR at λ=0.7** for diversity → **top-12** candidates.
- [ ] `app/vector/reranker.py` — pairwise rerank (cheap model, no prose) — bonus polish; can be stubbed
      pass-through initially.

### Agent tasks
- [ ] `app/agent/state.py` — `AgentState` TypedDict: `user_id, profile, evidence, queries, candidates,
      retrieval_score, refine_loops, draft, validation_errors, node_path`.
- [ ] `app/agent/schemas.py` — Pydantic structured outputs (`plan_queries`, `grade_retrieval`, `generate`
      shapes: `{headline, narrative, items:[{product_id, reason}]}`).
- [ ] `app/agent/prompts/` — versioned templates; keep the system prompt + catalog block **stable** so
      Mesh's response cache and prefix caching apply; only the behavior section varies.
- [ ] `app/agent/nodes/` — one file per node:
  1. `build_profile` *(no LLM)* — weighted top categories, search terms, level affinity, products seen.
  2. `plan_queries` *(cheap)* — behavior → 2–3 retrieval queries + metadata filter proposal.
  3. `retrieve` *(no LLM)* — hybrid retrieval above.
  4. `grade_retrieval` *(cheap, structured)* — score 0–1 + gap description + per-candidate keep/drop.
  5. `refine_queries` *(cheap)* — rewrite using the gap, widen/narrow filters, loop back to retrieve.
  6. `generate` *(quality, structured)* — headline + persuasive narrative that references *actual behavior*;
     each item's reason ties one product to one observed signal.
  7. `validate_grounding` *(no LLM)* — every `product_id` ∈ candidates **and** active in Postgres; 3–5 items,
     no duplicates; no price/claim absent from catalog metadata.
- [ ] `app/agent/graph.py` — wiring + conditional edges:
  - grade **≥ 0.7 → generate**; **< 0.7 AND refine_loops < 2 → refine_queries**; loops exhausted → generate;
  - validate fail → **1 retry with errors injected**; second fail → **deterministic top-K fallback**
    (similarity + templated narrative);
  - **hard caps: refine ≤ 2, one generation retry, 25s total timeout**;
  - LangGraph **Postgres checkpointer** (resumable/inspectable); write full `node_path` to `agent_runs`.
- [ ] `tests/test_grounding.py` — a generated item with an out-of-catalog `product_id` is rejected and the
      graph either retries or falls back; the system never emits an ungrounded item. **Invariant test.**

### Gotchas
Nodes 1, 3, 7 use **no LLM** — keep them the cheap backbone. An unbounded refine loop burns quota and hangs
the demo; the caps are non-negotiable. `node_path` on every run is what proves the graph is real.

### Definition of done
The graph runs end-to-end producing 3–5 grounded items with per-item reasons · refine loop is visible in a
LangSmith trace · caps enforced · `test_grounding.py` green · `node_path` recorded in `agent_runs`.

---

## Phase 7 — Trigger policy + three-layer cache (F5)

**Goal:** regenerate **only when it matters**, behind a cooldown and a lock, with two cache layers before a
full run. Target: **< 1 LLM generation per 40 events.** Explicitly judged.
**PRD:** §6.5 (+ Figure 6). **Invariant:** #6.

### Tasks
- [ ] `app/services/trigger.py` — `should_regenerate()`:
  `(events_since_gen ≥ 8 OR drift > 0.15 OR high-intent event) AND cooldown (> 10 min since last gen) AND
  Redis lock gen:{user_id} acquired`. Every early exit is a saved LLM call — log the reason.
- [ ] **L1 exact cache** — `profile_hash` unchanged → serve stored recommendation, no LLM.
- [ ] **L2 semantic cache** — cosine to cached profile > 0.95 → reuse items, cheap narrative
      re-personalization (1 small call).
- [ ] **Miss → full agent run** (L3 embedding cache via `content_hash` still applies).
- [ ] Invoke the trigger from the **event-ingest path after the batch is persisted** — never inside the
      hot request handler's LLM-free zone.
- [ ] `app/api/routes/recommendations.py` — `GET /api/recommendations/current`,
      `POST /api/recommendations/refresh` (rate-limited; demo affordance),
      `POST /api/recommendations/feedback` (`rec_id, item_id, signal` → writes a feedback event).
- [ ] Store recommendation + write `agent_runs` telemetry on every run.
- [ ] `tests/test_trigger.py` — below-threshold batch → no regeneration; L1/L2 hits skip the full run;
      cooldown + lock prevent concurrent runs for one user. **Invariant test.**

### Definition of done
Regeneration fires only on the compound condition · L1/L2 short-circuits measurably reduce LLM calls ·
concurrent triggers for one user are serialized by the Redis lock · `test_trigger.py` green.

---

## Phase 8 — Dashboard + surfacing (F6)

**Goal:** `/dashboard` shows the current recommendation and *visibly changes* as behavior changes.
**PRD:** §6.6, plus catalog/product/search/admin UI shells.

### Tasks
- [ ] `/dashboard` — headline, narrative, product cards each with a **"why this"** line; **transparency
      strip** ("Based on 14 actions · 3 searches · updated 2m ago"); expandable **"what we noticed about
      you"** panel (top categories); **thumbs up/down** → feedback event feeding the next run; **skeleton
      loading, never a blocking spinner**.
- [ ] Catalog / product-detail / search pages wired to the tracker (every search is an event; semantic search
      with keyword fallback).
- [ ] Admin UI — product CRUD + a `sync-status` readout.
- [ ] Frontend auth flow (login/register, credentialed fetch).
- [ ] Show the "Updated just now · based on your last 14 actions" affordance after a trigger cycle (U4).

### Definition of done
Three demo personas produce three visibly different, grounded recommendation blocks · the block updates
within one trigger cycle after a behavior shift · transparency strip + "what we noticed" render · thumbs
feedback writes an event.

---

## Phase 9 — Bonuses (drop first if behind)

**PRD:** §6.7 (F7), §6.8 (F8), §10 (evals). Each bonus README line must link to the exact implementing file.

### 9a — Scheduled proactive delivery (F7)
- [ ] `app/scheduler/jobs.py` — APScheduler `AsyncIOScheduler` with a **Postgres jobstore** (jobs survive
      restart): **09:00 daily** digest for opted-in users active in last 24h (`trigger_reason='scheduled'`);
      **03:00 daily** vector drift audit → alert admin console. (5s outbox drain already in Phase 4.)
- [ ] `app/scheduler/digest.py` + `templates/` — Jinja2 HTML email via SMTP or Resend; recap with a hook
      ("Yesterday you spent 20 minutes in advanced RAG…"); unsubscribe via `digest_optin`. Optional Telegram
      as a second channel.
- [ ] `scripts/send_digest_now.py` — fire a digest on demand (demo without waiting for 09:00).
- [ ] **Scheduler trap (§13.3):** free hosts sleep on idle → in-process 09:00 never fires. Deploy path uses a
      **GitHub Actions scheduled workflow → token-protected `POST /api/internal/run-digest`**. Keep
      APScheduler for local runs + the continuous 5s drain. Document which mode the deployed instance uses.

### 9b — Observability (F8)
- [ ] LangSmith tracing on every graph run under project `smartreco`, tagged `user_id` + `trigger_reason`.
- [ ] `GET /api/admin/agent-runs` — table of runs: node path, retrieval score, refine loops, tokens, cost,
      latency, LangSmith deep link. Screenshot in README.
- [ ] `GET /api/admin/metrics` — `llm_calls_per_100_events`, `cache_hit_rate`, `cost`.
- [ ] `run_id` correlation: API log line ↔ agent node logs ↔ Mesh call log.

### 9c — Evaluation harness (§10)
- [ ] `evals/dataset.json` — 10 synthetic behavior profiles.
- [ ] `evals/eval_recommendations.py` — score each generated set on: **Groundedness 100%**, **Behavioral
      relevance ≥ 0.7** (category overlap), **Persuasion quality ≥ 4.0** (LLM-as-judge via Mesh),
      **Diversity ≥ 2** (unique categories). Writes `evals/results.md`; commit the real numbers.

### 9d — Polish
- [ ] Enable the reranker (Phase 6) with measured lift.
- [ ] `scripts/simulate_behavior.py` — replays 3 personas against a running instance so recommendations
      visibly change in ~60s with no manual clicking (document as the first "try it" step).

### Definition of done (bonuses)
Each shipped bonus has a README line linking its file · digest lands in an inbox via `send_digest_now.py` ·
`agent-runs` + LangSmith trace visible · `results.md` committed with measured numbers.

---

## Phase 10 — README, deployment, demo & final push

**PRD:** §13 (deploy), §14 (README outline), §15 (rubric traceability).

### README (§14 — what judges read first)
1. One-paragraph pitch + 30s GIF of recommendations changing as behavior changes.
2. Architecture diagram.
3. Quickstart in <5 commands: `cp .env.example .env` → `docker compose up` →
   `python scripts/seed_data.py` → `python scripts/simulate_behavior.py` → open dashboard.
4. Bonus checklist — each item linking the exact file.
5. Efficiency section with the **measured** metrics table (LLM calls / 100 events, cache hit rate, median
   cost/rec, p95 latency).
6. Design decisions & trade-offs (outbox, per-node routing, Postgres+Qdrant, out-of-scope items).
7. Mesh compliance — point at `app/llm/mesh.py` as the single integration point.
8. Evaluation results.
9. Deployment notes — live URL, scheduler mode, known free-tier limits.
- [ ] Add the **Frontend note** (§2): why Next.js is compliant (Web Worker + sendBeacon reliability; backend
      is FastAPI per requirement); keep `requirements.txt` at repo root.
- [ ] State **non-goals** explicitly (payments, OAuth, multi-tenancy, A/B harness).

### Deployment (§13)
- [ ] **Prefer same-origin** (frontend served from the FastAPI container) to avoid the cross-site cookie trap
      (§13.2). If split: `SameSite=None; Secure` + credentialed CORS with an **explicit** origin list
      (wildcard rejected with credentials); verify `sendBeacon` sends cookies on the **deployed** URL.
- [ ] Topology (§13.1): Render/Railway (backend+scheduler), Vercel or same host (frontend), Neon/Supabase
      (Postgres), Qdrant Cloud (or same-host container), Upstash (Redis).
- [ ] Env vars per §13.4 as platform variables (never a committed file, never baked into the image); run
      migrations on boot + seed the catalog once.
- [ ] **Pre-demo checklist (§13.5)** against the deployed URL: `/health` all green · fresh register/login,
      cookie survives reload · browse 3 courses, events land · `sync-status` → `in_sync: true` · trigger a
      rec, it's grounded, `agent_runs` has a full node path · `send_digest_now.py` email arrives ·
      pre-generate the 3 demo personas so the recording never waits on a cold LLM call.

### Demo video (§13.6, 3 min)
Lead with the **behavior change** and the **grounding proof** — the two claims every submission makes and
few demonstrate. Follow the beat sheet: problem + live dashboard (0:00) → batched beacons in devtools
(0:20) → dashboard changed + "what we noticed" (0:50) → admin add product + `sync-status: in_sync: true`
(1:20) → `/admin/agent-runs` + LangSmith refine-loop trace (1:50) → digest email + metrics table (2:30).

### Final
- [ ] `python -m compileall app scripts evals tests -q` green · `pytest -q` green · CI green · repo public ·
      `.env` absent from history.

---

## Cut order

If you fall behind, drop bonuses in this exact sequence (§11 + CLAUDE.md):

`Telegram → LangSmith → evals harness → email digest → reranker`

**Never cut:** dual-write integrity · the LangGraph loop · grounding validation · Mesh routing.

---

## Rubric traceability (§15)

| Brief requirement | Satisfied in |
|---|---|
| Login with two roles | Phase 3 (§6.1) |
| Clean, related schema | Phase 1 (§5) |
| Admin product CRUD | Phase 4 (§6.2) |
| Dual-write, stores stay in sync | Phase 4 — outbox + drift audit (§6.2) |
| Efficient non-blocking tracking | Phase 5 (§6.3) |
| Sensible event schema | Phase 1 (`events`, §5) |
| Agent consumes activity + reasons | Phase 6 — nodes 1–2 (§6.4) |
| RAG over vector DB, grounded | Phase 6 — nodes 3 & 7 (§6.4) |
| Persuasive personalized narrative | Phase 6 — node 6 (§6.4) |
| Stored + refreshed recommendations | Phases 1 & 7 (§5, §6.5) |
| Don't call LLM on every action | Phase 7 — trigger + 3-layer cache (§6.5) |
| ⭐ Structured agent framework | Phase 6 — LangGraph, 7 nodes, conditional edges |
| ⭐ Scheduled proactive delivery | Phase 9a (§6.7) |
| ⭐ Observability | Phase 9b — LangSmith + `agent_runs` (§6.8) |
| ⭐ Retrieval polish | Phase 6 — hybrid, RRF, MMR, rerank, filters |
| Mesh API mandatory | Phase 2 — single client module (§7) |
| Optional deployed URL + demo video | Phase 10 (§13) |
