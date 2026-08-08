# SmartReco — Promptbook

One self-contained execution prompt per phase from `IMPLEMENTATION.md`. Each prompt is written **to a
subagent** that has repo access but may not have read the whole PRD. Every prompt:

1. names the docs to read first (so prompts don't drift from the spec),
2. restates the must-not-miss invariants, exact deliverable files, and numeric parameters inline,
3. specifies the required invariant test,
4. gives acceptance criteria and a **structured report-back contract** the orchestrator consumes.

**How to use:** feed a phase prompt verbatim to a subagent at the assigned model tier. The
[Orchestrator prompt](#orchestrator-prompt) at the end coordinates all phases. Authoritative sources for
every phase: `SmartReco-PRD.pdf`, `IMPLEMENTATION.md`, `CLAUDE.md`.

**Model assignment** (reasoning-heavy → Opus; well-specified/mechanical → Sonnet):

| Phase | 0 | 1 | 2 | 3 | 4 | 5 | 6 | 7 | 8 | 9 | 10 |
|---|---|---|---|---|---|---|---|---|---|---|---|
| Model | Sonnet | Sonnet | **Opus** | Sonnet | **Opus** | **Opus** | **Opus** | **Opus** | Sonnet | Opus(evals)/Sonnet | Sonnet |

**Universal rules (prepended mentally to every prompt):** All AI/embedding calls go through
`app/llm/mesh.py` only — never add a second provider SDK. Never write Qdrant from a request handler. No
secrets in the repo. Every schema change gets an Alembic migration. Before reporting done, run
`python -m compileall app scripts evals tests -q` **and** `pytest -q` and paste the results.

---

## Report-back contract (every phase returns this)

```
STATUS: done | blocked
FILES: <created/modified, one per line>
TESTS: <test files added + pass/fail counts>
VERIFY: compileall=<pass/fail> · pytest=<n passed/n failed> · invariant-test=<name: pass/fail>
INVARIANTS: <which invariants this phase touched and how each is upheld>
EXPORTS: <public functions/classes/endpoints the next phase depends on, with signatures>
DEVIATIONS: <anything done differently from IMPLEMENTATION.md, with reason>
FOLLOWUPS: <TODOs, known gaps, anything that blocks a later phase>
```

---

## Phase 0 — Scaffolding, infrastructure & CI  ·  model: Sonnet

You are a build engineer setting up the SmartReco repository skeleton so every later phase has a compiling,
runnable, CI-gated foundation.

**Read first:** `IMPLEMENTATION.md` → Phase 0; PRD §4, §9, §13.4; `CLAUDE.md` (all invariants + conventions).

**Preconditions:** none (first phase).

**Deliverables:**
- Full directory tree per PRD §9: `app/{main.py,core/,db/,api/routes/,services/,vector/,agent/,llm/,scheduler/}`,
  `scripts/`, `evals/`, `tests/`, `frontend/`. Keep **all** Python inside `app/ scripts/ evals/ tests/`.
- `docker-compose.yml` — postgres + qdrant + redis, healthchecks, named volumes; `docker compose up -d` is
  the only setup command.
- `requirements.txt` at repo root — **must** list `fastapi` (web framework) and `openai` (LLM client); this
  is a critical CI check. Include the dependency set from IMPLEMENTATION.md Phase 0.
- `.env.example` (committed, placeholder values for every var in PRD §13.4) and `.gitignore` (must contain
  `.env`, `node_modules/`, `*.db`, `__pycache__/`, Qdrant storage dirs).
- `app/core/config.py` (pydantic-settings `Settings`), `app/core/logging.py` (structured JSON + `run_id`
  helper), `app/core/security.py` (empty helper stubs for Phase 3).
- `app/main.py` — FastAPI app + `lifespan` hook (empty for now) + `GET /health` reporting db·qdrant·redis·mesh
  reachability.
- `.github/workflows/smartreco-checks.yml` — runs `python -m compileall app scripts evals tests -q` and
  `pytest -q` on every push.
- Fix doc drift: make `CLAUDE.md`'s `docs/SmartReco-PRD.md` reference resolve (add the path or update it).

**Constraints:** no secrets anywhere; no feature logic yet (stubs only); don't put Python under `frontend/`.

**Acceptance:** `docker compose up -d` → all healthy; `uvicorn app.main:app` boots; `GET /health` responds;
`compileall` + an empty `pytest` pass. Then emit the report-back contract.

---

## Phase 1 — Data model & migrations  ·  model: Sonnet

You are a database engineer defining the full SmartReco schema through Alembic only (no `create_all()` in app
code).

**Read first:** `IMPLEMENTATION.md` → Phase 1; PRD §5 including "Key DDL details"; `CLAUDE.md` DB conventions.

**Preconditions:** Phase 0 green.

**Deliverables:**
- `app/db/models.py` — SQLAlchemy models: `users`, `user_profiles`, `products`, `events`, `vector_outbox`,
  `agent_runs`, `recommendations` (fields exactly per PRD §5 ER model).
- `app/db/session.py` — async engine, `AsyncSession` factory, FastAPI dependency.
- `alembic/` initialized + initial migration capturing every constraint below.
- Qdrant bootstrap helper: collection `products`, **cosine**, **1536-dim**, payload
  `{product_id, title, category, tags, level, price_cents, is_active}`, **point ID = product_id**; created
  idempotently on startup.

**Must-not-drop specifics:**
- `events`: `UNIQUE (user_id, session_id, event_type, client_ts)`; `weight REAL NOT NULL DEFAULT 1.0`
  (server-assigned); index `(user_id, server_ts DESC)` + partial `INCLUDE (event_type, product_id) WHERE
  server_ts > now() - INTERVAL '7 days'`.
- `vector_outbox`: `op IN ('upsert','delete')`, `status IN ('pending','done','failed')`, `attempts`,
  `last_error`, `processed_at`; partial index `WHERE status='pending'`.
- `recommendations`: `UNIQUE ... WHERE is_current`.
- `products`: `content_hash`, `vector_synced_at`.
- `user_profiles`: `interest_vector real[]`, `top_categories jsonb`, `top_terms jsonb`, `events_since_gen`,
  `profile_hash`, `last_generated_at`.
- `agent_runs`: `node_path text[]`, `retrieval_score`, `refine_loops`, `cache_hit`, `models_used jsonb`,
  `cost_usd`, `latency_ms`, `langsmith_url`, `status`.
- Timestamps via `server_default=now()`.

**Acceptance:** `alembic upgrade head` builds all tables/indexes; `alembic downgrade base` is clean; Qdrant
collection created idempotently; models import under `compileall`. Emit report-back.

---

## Phase 2 — Mesh gateway (the single AI client)  ·  model: Opus

You are implementing the **only** LLM/embedding integration point in SmartReco. A submission that routes any
AI call outside Mesh is disqualified — treat this as the highest-stakes phase.

**Read first:** `IMPLEMENTATION.md` → Phase 2; PRD §7, §6.5, §6.5.1; `CLAUDE.md` invariants #1 and #7 +
"Mesh API client" notes.

**Preconditions:** Phase 0 green.

**Deliverables:**
- `app/llm/mesh.py` — the **only** `AsyncOpenAI(base_url="https://api.meshapi.ai/v1",
  api_key=settings.MESH_API_KEY)` construction in the whole codebase. Wrapper must provide: per-node model
  resolution via `model_router.resolve(node, tier)` (never hardcode a model ID at a call site); `tenacity`
  exponential backoff on 429/5xx; **20s timeout**; token + cost accounting written to `agent_runs`;
  Pydantic-validated JSON mode for structured outputs; embeddings via `/v1/embeddings`
  (`openai/text-embedding-3-small`, 1536-dim) with a `content_hash` cache; **`MESH_DISABLED=true`**
  short-circuit returning recorded fixtures for every node and for embeddings.
- `app/llm/model_router.py` — `FREE_PREFERENCE` list; `PAID_FALLBACK = {"cheap": "openai/gpt-4o-mini",
  "quality": "anthropic/claude-sonnet-4.5"}`; `free_model_ids()` reads `client.models.list()` `is_free` flag
  (`lru_cache`, resolved at startup); `resolve()` walks the chain then falls back to paid. Routing:
  `plan_queries`/`grade_retrieval`/`rerank` → cheap free instruct; `generate` → free large → quality;
  embeddings → cheap.
- `scripts/list_free_models.py` — 3-line wrapper over `client.models.list()` printing the resolved free catalog.
- `tests/test_single_gateway.py` — import-lint asserting **exactly one** client construction site and **no**
  banned provider SDK (`groq`, `openrouter`, `anthropic`, `google-generativeai`, `together`,
  `sentence-transformers`, `fastembed`) in code imports or `requirements.txt`.

**Constraints:** never hardcode a model ID at a call site; never weaken `test_single_gateway.py`; no runtime
non-Mesh embedding path.

**Acceptance:** `MESH_DISABLED=true` runs the app with zero AI network calls; `list_free_models.py` prints a
catalog; all model choice flows through `resolve()`; `test_single_gateway.py` green. Emit report-back
including the exact `mesh` call signatures the agent nodes will use.

---

## Phase 3 — Authentication (F1)  ·  model: Sonnet

You are implementing simple, correct email/password auth with roles (no OAuth).

**Read first:** `IMPLEMENTATION.md` → Phase 3; PRD §6.1, §8 (auth routes); `CLAUDE.md` §13.2 cookie notes.

**Preconditions:** Phase 1 green.

**Deliverables:**
- `app/core/security.py` — bcrypt **cost 12**; JWT **HS256, 24h** with `role` claim; encode/decode helpers.
- `app/api/routes/auth.py` — `POST /api/auth/register` (email, password, display_name),
  `POST /api/auth/login` (sets httpOnly JWT cookie), `GET /api/auth/me`.
- Cookie: httpOnly, `SameSite` from config (`lax` local / `none;secure` cross-origin).
- `require_role("admin")` FastAPI dependency for admin routes.
- `tests/test_auth.py` — register→login→me round-trip; admin guard rejects non-admin; password never
  returned; token carries role.

**Constraints:** never return password hashes; JWT secret from config only; no plaintext passwords in logs.

**Acceptance:** auth round-trip works; admin dependency blocks non-admins; tests green; `compileall`+`pytest`
pass. Emit report-back including the `require_role` import path and the current-user dependency signature.

---

## Phase 4 — Catalog + transactional outbox dual-write (F2)  ·  model: Opus  ·  ★ NEVER CUT

You are implementing the submission's core differentiator: product CRUD that keeps Postgres and Qdrant
**provably** in sync via a committed outbox. Never call Qdrant inside a request handler.

**Read first:** `IMPLEMENTATION.md` → Phase 4; PRD §6.2 + Figure 3; `CLAUDE.md` invariant #3 + "The outbox"
notes.

**Preconditions:** Phases 1, 2, 3 green (needs schema, Mesh embeddings, admin auth).

**Deliverables:**
- `app/services/catalog.py` — create/update/delete a product **and** insert the matching `vector_outbox` row
  (`op='upsert'|'delete'`) in **one transaction**; update recomputes `content_hash`; delete is soft
  (`is_active=false`) + outbox `delete`. Handler returns fast with **no vector call in the request path**.
- `app/vector/qdrant_client.py` — thin wrapper behind an interface.
- `app/vector/embeddings.py` — Mesh `/v1/embeddings` + `content_hash` cache (skip re-embed when unchanged).
- `app/services/outbox_worker.py` — drain **every 5s**: `SELECT ... FOR UPDATE SKIP LOCKED` claims pending
  rows; skip re-embed on unchanged `content_hash`; upsert/delete Qdrant point (id = product_id); success →
  `status='done'` + `products.vector_synced_at=now()`; failure → `attempts++`, exponential backoff, mark
  `failed` after 5 attempts and surface to admin.
- Wire the 5s drain into APScheduler in `app/main.py` lifespan (in-process, continuous).
- `app/api/routes/admin.py` — `POST/PATCH/DELETE /api/admin/products` (admin-guarded);
  `GET /api/admin/sync-status` → `{in_sync, missing_in_vector, orphaned_in_vector, outbox_lag_seconds}`,
  returning `in_sync: true` on a healthy system.
- `app/api/routes/products.py` — `GET /api/products?category&level&q&page`, `GET /api/products/{id}`.
- `scripts/seed_data.py` — 40 courses + 1 admin + **3 demo users with pre-baked divergent behavior histories**.
- `tests/test_dual_write.py` — create writes product + pending outbox row in one tx; worker drains it;
  `sync-status` → `in_sync: true`; unchanged update skips re-embed; delete removes the Qdrant point.

**Constraints:** absolutely no Qdrant call in any request handler; the product+outbox writes must share one
transaction (all-or-nothing); the unique/skip-locked claiming must be concurrent-safe.

**Acceptance:** admin CRUD lands in Postgres then Qdrant within ≤1 drain cycle; `sync-status: in_sync: true`;
re-embed skipped when unchanged; `test_dual_write.py` green; seed populates 40 courses + 4 users. Emit
report-back. Treat any `sync-status` failure as P0.

---

## Phase 5 — Behavioral tracking + profile builder (F3)  ·  model: Opus

You are implementing provably non-blocking, batched, lossless client tracking → server-weighted events →
incremental interest profile. The tracker is the one place performance genuinely matters.

**Read first:** `IMPLEMENTATION.md` → Phase 5; PRD §6.3 + Figure 4; `CLAUDE.md` "Event tracking" notes.

**Preconditions:** Phases 1, 4 green (needs schema + products to track).

**Deliverables (backend):**
- `app/api/routes/events.py` — `POST /api/events/batch` → Pydantic validate → **202 Accepted** →
  `BackgroundTask` single bulk insert/COPY.
- `app/services/event_ingest.py` — bulk insert with **server-side weighting**: view 1.0 · click 1.5 ·
  search 2.0 · dwell>30s 2.5 · cart 3.0 (never trust a client weight); natural key dedupes retries.
- `app/services/profile.py` — incremental profile: **decayed interest centroid, 3-day half-life**,
  `top_categories`, `top_terms`, level affinity, recompute `profile_hash`, bump `events_since_gen`. No LLM call.

**Deliverables (frontend):**
- Scaffold `frontend/` (Next.js App Router, TypeScript strict, Tailwind; server components by default).
- `frontend/lib/tracker.ts` — ring buffer **cap 200, drop-oldest** in a **Web Worker**; flush at **20 events
  / 10s / whichever first** + `visibilitychange → hidden` via `navigator.sendBeacon`; scroll **throttled
  1/500ms** → milestones 25/50/75/100; search **debounced 400ms**; dwell via `IntersectionObserver`
  (foreground only, one event on unmount); failed batches → `localStorage` spillover replayed next load;
  client-generated event UUID for idempotency. Never block the main thread; no synchronous XHR.
- `tests/test_tracker_load.py` — Playwright/k6 firing **1,000 events**, asserting p95 client handler time
  **< 2 ms** with **zero dropped events**.

**Constraints:** the ingest path must never call the LLM; the server, not the client, assigns `weight`.

**Acceptance:** events batch, land with correct weights, dedupe on retry; profile updates with a decayed
centroid; tracker is non-blocking and survives tab close; 1,000-event load test passes. Emit report-back
including the profile object shape and `events_since_gen`/`profile_hash`/`drift` accessors Phase 7 needs.

---

## Phase 6 — LangGraph agent + retrieval + grounding (F4)  ·  model: Opus  ·  ★ NEVER CUT

You are implementing the centerpiece: a real seven-node LangGraph with conditional edges, self-grading, a
bounded refine loop, and a structural grounding gate. The system must **never** show an ungrounded
recommendation.

**Read first:** `IMPLEMENTATION.md` → Phase 6; PRD §6.4 + Figure 5; `CLAUDE.md` "The agent" notes; the Mesh
call signatures from Phase 2 and the profile shape from Phase 5.

**Preconditions:** Phases 2, 4, 5 green (needs Mesh, synced vectors, profile).

**Deliverables:**
- `app/vector/hybrid_retriever.py` — per query: dense vector + **BM25**, fused with **Reciprocal Rank
  Fusion**, metadata-filtered, then **MMR at λ=0.7** → **top-12** candidates.
- `app/vector/reranker.py` — pairwise rerank (cheap model, no prose); may start as pass-through.
- `app/agent/state.py` — `AgentState` TypedDict: `user_id, profile, evidence, queries, candidates,
  retrieval_score, refine_loops, draft, validation_errors, node_path`.
- `app/agent/schemas.py` — Pydantic structured outputs; `generate` → `{headline, narrative, items:[{product_id, reason}]}`.
- `app/agent/prompts/` — versioned templates; keep system prompt + catalog block **stable** (only behavior
  section varies) so Mesh prefix/response caching applies.
- `app/agent/nodes/` — one file per node:
  1. `build_profile` *(no LLM)*; 2. `plan_queries` *(cheap)* → 2–3 queries + filter proposal;
  3. `retrieve` *(no LLM)* → hybrid above; 4. `grade_retrieval` *(cheap, structured)* → score 0–1 + gap +
  keep/drop; 5. `refine_queries` *(cheap)* → rewrite using gap, widen/narrow, loop back; 6. `generate`
  *(quality, structured)* → narrative referencing actual behavior, each item's reason ties one product to one
  observed signal; 7. `validate_grounding` *(no LLM)* → every `product_id` ∈ candidates **and** active in
  Postgres, 3–5 items, no duplicates, no price/claim absent from catalog metadata.
- `app/agent/graph.py` — conditional edges: grade **≥0.7 → generate**; **<0.7 AND refine_loops<2 → refine**;
  loops exhausted → generate; validate fail → **1 retry with errors injected**; second fail → **deterministic
  top-K fallback** (similarity + templated narrative). **Hard caps: refine ≤2, one generation retry, 25s
  timeout.** LangGraph Postgres checkpointer; write full `node_path` to `agent_runs`.
- `tests/test_grounding.py` — an out-of-catalog `product_id` is rejected and the graph retries or falls back;
  the system never emits an ungrounded item.

**Constraints:** nodes 1/3/7 use **no LLM**; the caps are non-negotiable (unbounded loops burn quota and hang
the demo); no path that returns raw LLM output bypassing `validate_grounding`.

**Acceptance:** graph produces 3–5 grounded items with per-item reasons; refine loop is visible in the trace;
caps enforced; `test_grounding.py` green; `node_path` recorded. Emit report-back including the callable entry
point (e.g. `run_agent(user_id) -> Recommendation`) Phases 7/8 will call.

---

## Phase 7 — Trigger policy + three-layer cache (F5)  ·  model: Opus

You are implementing the cost gate: regenerate **only when it matters**, behind a cooldown and a distributed
lock, with two cache layers before a full agent run. This is explicitly judged. Target: **< 1 LLM generation
per 40 events.**

**Read first:** `IMPLEMENTATION.md` → Phase 7; PRD §6.5 + Figure 6; `CLAUDE.md` "Trigger policy" notes; the
agent entry point from Phase 6 and profile accessors from Phase 5.

**Preconditions:** Phases 5, 6 green.

**Deliverables:**
- `app/services/trigger.py` — `should_regenerate()`: `(events_since_gen ≥ 8 OR drift > 0.15 OR high-intent
  event) AND cooldown (>10 min since last gen) AND Redis lock gen:{user_id} acquired`. Log every early-exit
  reason (each is a saved LLM call).
- **L1 exact cache**: `profile_hash` unchanged → serve stored recommendation, no LLM.
- **L2 semantic cache**: cosine to cached profile > 0.95 → reuse items + cheap narrative re-personalization
  (1 small call).
- **Miss → full agent run** (L3 embedding cache via `content_hash` still applies).
- Invoke the trigger from the **event-ingest path after the batch is persisted** (not inside the hot handler).
- `app/api/routes/recommendations.py` — `GET /api/recommendations/current`,
  `POST /api/recommendations/refresh` (rate-limited demo affordance),
  `POST /api/recommendations/feedback` (`rec_id, item_id, signal` → writes a feedback event).
- Store recommendation + write `agent_runs` telemetry on every run.
- `tests/test_trigger.py` — below-threshold batch → no regeneration; L1/L2 hits skip the full run; cooldown +
  lock prevent concurrent runs for one user.

**Constraints:** never call the LLM inside the event ingest request handler; the Redis lock must prevent two
concurrent agent runs for the same user.

**Acceptance:** regeneration fires only on the compound condition; L1/L2 measurably cut LLM calls; concurrent
triggers serialize; `test_trigger.py` green. Emit report-back including the measured LLM-calls-per-100-events
figure from a `simulate_behavior` run if available.

---

## Phase 8 — Dashboard + surfacing (F6)  ·  model: Sonnet

You are building the UI where the current recommendation renders and **visibly changes** as behavior changes.

**Read first:** `IMPLEMENTATION.md` → Phase 8; PRD §6.6; the recommendation shape from Phases 6/7; the
tracker from Phase 5.

**Preconditions:** Phases 5, 6, 7 green.

**Deliverables:**
- `/dashboard` — headline, narrative, product cards each with a **"why this"** line; **transparency strip**
  ("Based on 14 actions · 3 searches · updated 2m ago"); expandable **"what we noticed about you"** panel
  (top categories); **thumbs up/down** → feedback event; **skeleton loading, never a blocking spinner**;
  the "Updated just now · based on your last 14 actions" affordance after a trigger cycle (U4).
- Catalog / product-detail / search pages wired to the tracker (every search is an event; semantic search
  with keyword fallback).
- Admin UI — product CRUD + a `sync-status` readout.
- Frontend auth flow (login/register, credentialed fetch).

**Constraints:** server components by default, `"use client"` only where interactivity requires it; TypeScript
strict; never block on a spinner.

**Acceptance:** three demo personas render three visibly different grounded blocks; the block updates within
one trigger cycle after a behavior shift; transparency strip + "what we noticed" render; thumbs writes an
event. Emit report-back.

---

## Phase 9 — Bonuses  ·  model: Opus (evals) / Sonnet (rest)  ·  drop first if behind

You are adding the bonus features, each with a README line linking its exact file. These sub-tasks are
independent and may run in parallel.

**Read first:** `IMPLEMENTATION.md` → Phase 9; PRD §6.7, §6.8, §10, §13.3.

**Preconditions:** Phases 6, 7 green.

**9a — Scheduled delivery (F7)  ·  Sonnet:** `app/scheduler/jobs.py` (APScheduler `AsyncIOScheduler` +
Postgres jobstore): **09:00** digest for opted-in users active in last 24h (`trigger_reason='scheduled'`);
**03:00** drift audit → admin alert. `app/scheduler/digest.py` + `templates/` (Jinja2 HTML via SMTP/Resend,
recap-with-hook, unsubscribe via `digest_optin`; optional Telegram). `scripts/send_digest_now.py`.
**Scheduler trap:** deployed digest fires via a **GitHub Actions scheduled workflow → token-protected
`POST /api/internal/run-digest`**; keep APScheduler for local + the continuous 5s drain; document the mode.

**9b — Observability (F8)  ·  Sonnet:** LangSmith tracing on every run under project `smartreco`, tagged
`user_id` + `trigger_reason`. `GET /api/admin/agent-runs` (node path, retrieval score, refine loops, tokens,
cost, latency, LangSmith deep link). `GET /api/admin/metrics` (`llm_calls_per_100_events`, `cache_hit_rate`,
`cost`). `run_id` correlation across API/node/Mesh logs.

**9c — Eval harness (§10)  ·  Opus:** `evals/dataset.json` (10 synthetic profiles);
`evals/eval_recommendations.py` scoring Groundedness (100%), Behavioral relevance (≥0.7 category overlap),
Persuasion quality (≥4.0, LLM-as-judge via Mesh), Diversity (≥2 unique categories); writes `evals/results.md`
with real numbers.

**9d — Polish  ·  Sonnet:** enable the reranker with measured lift; `scripts/simulate_behavior.py` replays 3
personas so recommendations change in ~60s with no clicking.

**Acceptance:** each shipped bonus has a README line linking its file; digest lands in an inbox via
`send_digest_now.py`; `agent-runs` + a LangSmith trace are visible; `results.md` committed with measured
numbers. Emit report-back per sub-task.

---

## Phase 10 — README, deployment, demo & final push  ·  model: Sonnet (Opus final review)

You are producing the judge-facing deliverables and shipping.

**Read first:** `IMPLEMENTATION.md` → Phase 10; PRD §13, §14, §15; the metrics from Phases 7/9.

**Preconditions:** all prior phases green.

**Deliverables:**
- `README.md` per PRD §14 (9 sections): pitch + 30s GIF, architecture diagram, <5-command quickstart, bonus
  checklist (each linking its file), **measured** efficiency metrics table, design decisions & trade-offs,
  Mesh compliance pointing at `app/llm/mesh.py`, evaluation results, deployment notes (live URL, scheduler
  mode, free-tier limits). Include the Frontend note (why Next.js is compliant) and explicit non-goals.
- Deployment (PRD §13): prefer same-origin; if split, `SameSite=None; Secure` + credentialed CORS with an
  explicit origin list; run migrations on boot + seed once; env vars as platform variables.
- Pre-demo checklist (§13.5) executed **against the deployed URL**.
- Demo video script (§13.6, 3 min) — lead with the behavior change and the grounding proof.

**Constraints:** README is the one deliverable doc; every bonus line must link the exact file; no secrets in
history.

**Acceptance:** `compileall` + `pytest -q` + CI green; repo public; `.env` absent from history; rubric-
traceability table (§15) present. Emit report-back.

---

## Orchestrator prompt

Paste the block below to launch the coordinated build. It makes Opus the orchestrator and dispatches
Opus/Sonnet subagents per the model-assignment table above.

```
You are the ORCHESTRATOR and tech lead for building SmartReco, a hackathon submission. You do NOT write
feature code yourself — you plan, dispatch subagents, verify their work against the invariants, integrate,
and commit. Your job is correctness and sequencing, not typing.

AUTHORITATIVE DOCS (read before dispatching): SmartReco-PRD.pdf, IMPLEMENTATION.md, PROMPTBOOK.md, CLAUDE.md.
The invariants in CLAUDE.md are law. If any subagent's work would break one, STOP and flag it — do not
proceed or "temporarily" weaken a test.

THE INVARIANTS (never violate, never let a subagent violate):
1. Every LLM/embedding call goes through app/llm/mesh.py only; no second provider SDK anywhere.
2. Recommendations are grounded; validate_grounding rejects any product_id not in candidates — no bypass.
3. Never write Qdrant from a request handler; product + vector_outbox commit in one transaction.
4. No secrets in the repo. 5. All Python compiles. 6. No LLM call on every event (trigger gate).

MODEL ASSIGNMENT (pass via the Agent tool's `model` param):
  Phase 0 Sonnet · 1 Sonnet · 2 Opus · 3 Sonnet · 4 Opus · 5 Opus · 6 Opus · 7 Opus · 8 Sonnet
  Phase 9: 9c(evals) Opus, 9a/9b/9d Sonnet · Phase 10 Sonnet (then an Opus review pass)

DEPENDENCY ORDER (DAG — respect it):
  0 → 1 ; 0 → 2 ; 1 → 3 ; {1,2,3} → 4 → 5 → 6 → 7 → 8 → 10 ; {6,7} → 9 → 10
  Parallelizable: run 2 and 3 concurrently after their deps; run 9a/9b/9c/9d concurrently.

PER-PHASE LOOP:
  1. Confirm the phase's precondition phases are DONE and their verification is green.
  2. Spawn a subagent with the Agent tool: subagent_type "general-purpose", model = assigned tier, and the
     task = that phase's prompt from PROMPTBOOK.md VERBATIM. For parallel phases, launch them in one message
     (multiple tool calls) with run_in_background: true. For phases where subagents edit files concurrently,
     use isolation: "worktree".
  3. When it returns, DO NOT trust its self-report. YOU run the verification gate yourself:
       - `python -m compileall app scripts evals tests -q`
       - `pytest -q`
       - the phase's named invariant test (test_single_gateway / test_dual_write / test_grounding /
         test_trigger) as applicable.
  4. For the critical phases (2, 4, 6, 7), spawn a SEPARATE Opus reviewer subagent to adversarially verify
     the invariant (single-gateway / dual-write sync / grounding / trigger gating) — prompt it to try to
     BREAK the guarantee, not confirm it. Only accept when it cannot.
  5. Green + reviewed → commit (small, descriptive message) and move on. Red → send the exact failure output
     back to the SAME subagent via SendMessage to fix, then re-verify. After 2 failed Sonnet attempts,
     escalate that phase to Opus.
  6. After each phase, report progress to me in 3-4 lines: what shipped, verification result, what's next.

GUARDRAILS: never add a dependency without a one-sentence reason; never weaken or skip test_single_gateway;
keep all Python under app/ scripts/ evals/ tests/. Cut order if we fall behind:
Telegram → LangSmith → evals → digest → reranker; never cut dual-write, the LangGraph loop, grounding, or
Mesh routing.

Begin with Phase 0. Confirm the plan and the first dispatch, then proceed autonomously through the DAG,
pausing only to surface an invariant risk or a blocked phase.
```
