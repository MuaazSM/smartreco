# CLAUDE.md

Guidance for Claude Code when working in this repository.

---

## What this project is

SmartReco is a hackathon submission for the **SmartReco Build Challenge 2026** (Krish Naik Academy × Mesh API), due **11 August 2026**.

It is a course-marketplace web app that tracks user behavior, feeds it to a LangGraph agent, retrieves matching products from a vector database via RAG, and generates persuasive, personalized recommendations that update as behavior changes.

The full specification lives in `SmartReco-PRD.pdf` (repo root). **Read the relevant PRD section before implementing a component.** This file covers how to work in the repo; the PRD covers what to build.

---

## Hard invariants — never violate these

These are submission-invalidating or score-destroying. If a change would break one, stop and flag it rather than proceeding.

1. **Every LLM/AI call goes through Mesh API.** The only place an LLM client may be constructed is `app/llm/mesh.py`. Do not add `groq`, `openrouter`, `anthropic`, `google-generativeai`, `together` or any other provider SDK to `requirements.txt` or import one anywhere. A submission that does not route through Mesh is disqualified outright. `tests/test_single_gateway.py` enforces this — do not weaken or skip it.

2. **Never invent product data.** Recommendations must reference real rows from the catalog. `validate_grounding` rejects any `product_id` not in the retrieved candidate set. Do not add a "if validation fails, just return what the LLM said" path.

3. **Never write to Qdrant from inside a request handler.** Product writes go to Postgres and the `vector_outbox` table in one transaction; the worker drains the outbox. This is the core differentiator of the submission.

4. **No secrets in the repo.** `.env` is gitignored. `.env.example` holds keys with empty or placeholder values. Never hardcode `rsk_...`, JWT secrets, SMTP passwords, or connection strings — not in code, not in tests, not in docs, not in a comment "for now."

5. **All Python must compile.** CI runs a syntax check across every `.py` file and a failure blocks evaluation eligibility. Run `python -m compileall app scripts evals tests -q` before declaring work done.

6. **Do not call the LLM on every event.** Regeneration passes through the trigger policy in `app/services/trigger.py`. If you find yourself adding an LLM call inside the event ingest path, you have misread the design.

7. **Child of #1:** embeddings are also AI calls. They go through Mesh's `/v1/embeddings`. Do not add `sentence-transformers` or FastEmbed as a runtime path.

---

## Commands

```bash
# Environment
cp .env.example .env                    # then fill in MESH_API_KEY
docker compose up -d                    # postgres + qdrant + redis
pip install -r requirements.txt

# Database
alembic upgrade head                    # migrations
python scripts/seed_data.py             # 40 courses + admin + 3 demo users

# Run
uvicorn app.main:app --reload           # backend on :8000
cd frontend && npm run dev              # frontend on :3000

# Development without burning Mesh quota
MESH_DISABLED=true uvicorn app.main:app --reload   # returns fixtures

# Verification
python -m compileall app scripts evals tests -q    # CI critical check
pytest -q
python scripts/simulate_behavior.py                # replay 3 personas
python scripts/list_free_models.py                 # which Mesh models are free
python evals/eval_recommendations.py               # writes evals/results.md
```

---

## Repository layout

```
app/
  main.py                 FastAPI app, lifespan starts APScheduler
  core/                   config (pydantic-settings), security, logging
  db/                     SQLAlchemy models, session, alembic/
  api/routes/             auth, products, events, recommendations, admin
  services/
    catalog.py            product CRUD + outbox write (one transaction)
    outbox_worker.py      drains vector_outbox every 5s
    event_ingest.py       bulk insert, server-side weighting
    profile.py            decayed interest centroid, profile_hash
    trigger.py            should_regenerate() — the cost gate
  vector/
    qdrant_client.py      thin wrapper; keep behind an interface
    embeddings.py         Mesh /v1/embeddings + content_hash cache
    hybrid_retriever.py   dense + BM25 → RRF → filter → MMR
  agent/
    graph.py              LangGraph wiring, conditional edges
    state.py              AgentState TypedDict
    nodes/                one file per node
    schemas.py            Pydantic structured outputs
    prompts/              prompt templates, versioned
  llm/
    mesh.py               THE ONLY LLM CLIENT
    model_router.py       free-tier resolution
  scheduler/              jobs.py, digest.py, templates/
frontend/                 Next.js App Router; lib/tracker.ts is the important file
scripts/                  seed_data, simulate_behavior, send_digest_now, list_free_models
evals/                    eval_recommendations.py, dataset.json, results.md
tests/
SmartReco-PRD.pdf         the specification (repo root)
```

---

## Conventions

**Python.** 3.11. Async throughout — `async def` routes, `AsyncSession`, `AsyncOpenAI`. Type hints on every function signature. Pydantic v2 for all request/response and structured-output schemas. Line length 100.

**Errors.** Raise `HTTPException` from routes; let services raise domain exceptions and translate at the boundary. Never swallow an exception silently — log it with the `run_id`.

**Logging.** Structured JSON via `app/core/logging.py`. Every agent run gets a `run_id` that appears in the API log line, the agent node logs, and the Mesh call log. This is what makes debugging possible at 2am on day 3.

**Database.** Alembic for every schema change — no `create_all()` in application code. Use `server_default` rather than Python defaults for timestamps. Index anything you filter or sort on.

**Frontend.** TypeScript strict. Tailwind. Server components by default, `"use client"` only where interactivity requires it. The tracker is the one place where performance genuinely matters — keep queue work off the main thread.

**Tests.** Pytest with `pytest-asyncio`. The four that matter most are `test_dual_write.py`, `test_grounding.py`, `test_trigger.py`, `test_single_gateway.py`. Write these even if coverage elsewhere is thin — they defend the invariants above.

**Commits.** Use [Conventional Commits](https://www.conventionalcommits.org) (`feat:`, `fix:`, `chore:`, `refactor:`, `docs:`, `test:`, `ci:`, `build:`). Keep messages short and descriptive of *what changed*, not internal build phases — never put a phase name, task label, or step number in the message. Commit incrementally at sub-phase granularity (small, focused commits), and push after each. **No trailers** — no `Co-Authored-By`, no session links, no tooling attribution. Author every commit as `MuaazSM <170370120+MuaazSM@users.noreply.github.com>` (the repo owner). Remote is `origin` → `https://github.com/MuaazSM/smartreco.git`, branch `main`. CI runs on every push and green CI is an eligibility requirement.

---

## Component notes and gotchas

### Mesh API client (`app/llm/mesh.py`)

```python
client = AsyncOpenAI(base_url="https://api.meshapi.ai/v1", api_key=settings.MESH_API_KEY)
```

Wrap all calls with: per-node model resolution via `model_router.resolve()`, `tenacity` retry with exponential backoff on 429/5xx, a 20s timeout, and token/cost accounting written to `agent_runs`. Never hardcode a model ID at a call site — the router owns model choice.

Model IDs on the free tier change. `model_router` walks a preference list and falls back to a paid model; it resolves at startup from `client.models.list()` reading the `is_free` flag. If a model 404s, the fix is the preference list, not a hardcoded ID at the call site.

### The outbox (`app/services/catalog.py`, `outbox_worker.py`)

The transaction boundary is the whole point. Product row and outbox row commit together or not at all. The worker claims rows with `SELECT ... FOR UPDATE SKIP LOCKED` so concurrent workers don't double-process. On update, recompute `content_hash` and skip re-embedding when unchanged.

`GET /api/admin/sync-status` must return `in_sync: true` on a healthy system. Treat a failure here as a P0 — it is the single most demonstrable claim in the submission.

### Event tracking (`frontend/lib/tracker.ts`)

Ring buffer in a Web Worker, capped at 200 with drop-oldest. Flush at 20 events or 10 seconds, whichever first, plus `visibilitychange → hidden` via `navigator.sendBeacon`. Scroll throttled to 1/500ms and reduced to milestone depths; search debounced 400ms. Never block the main thread; never use synchronous XHR.

Server assigns `weight` by event type — never trust a client-supplied weight. The `(user_id, session_id, event_type, client_ts)` unique constraint makes retries idempotent; do not remove it.

### The agent (`app/agent/`)

Seven nodes: `build_profile → plan_queries → retrieve → grade_retrieval → [refine_queries ↺] → generate → validate_grounding`. Conditional edges on the grade score (≥0.7 proceeds) and on validation failure (one retry, then deterministic fallback).

Hard caps: `refine_loops ≤ 2`, one generation retry, 25s total timeout. An unbounded loop here will burn quota and hang the demo. `node_path` must be recorded on every run — it is what proves the graph is real rather than decorative.

Nodes 1, 3 and 7 use no LLM at all. Keep it that way; they are the cheap backbone.

### Trigger policy (`app/services/trigger.py`)

Regenerate only when: (8+ new events OR drift > 0.15 OR a high-intent event) AND cooldown elapsed AND the Redis lock is acquired. Then L1 exact cache, then L2 semantic cache, then a full run. Every early exit is a saved LLM call — that is a judged criterion, not an optimization.

### Deployment

See PRD §13. Two traps that only appear once deployed:

- **Cookies.** `SameSite=Lax` does not survive cross-site requests. Prefer a same-origin topology. If frontend and backend are split, use `SameSite=None; Secure` plus credentialed CORS with an explicit origin list — a wildcard origin is rejected by browsers when credentials are enabled.
- **Scheduler.** Free hosts sleep on idle, so in-process APScheduler never fires the 09:00 digest. Use a GitHub Actions scheduled workflow calling a token-protected endpoint. Keep APScheduler for the outbox drain, which must be continuous.

---

## What NOT to do

- Do not stub features to make a demo work. Hardcoded recommendations, a vector DB that is never queried, or an LLM client that is never called are explicitly called out in the rules as scoring poorly. A working simple version beats a faked sophisticated one.
- Do not add a second LLM provider "just for development." Use `MESH_DISABLED=true` fixtures instead.
- Do not put Python outside `app/`, `scripts/`, `evals/`, `tests/`. Stray files in the frontend tree can trip the CI compile check.
- Do not commit `.env`, `node_modules/`, `*.db`, `__pycache__/`, or Qdrant storage directories.
- Do not refactor working code for elegance while features remain unbuilt. The deadline is 11 August.
- Do not add dependencies without a reason you can state in one sentence.
- Do not create documentation files unless asked. The README is the one deliverable doc.

---

## Priority order if time runs short

Build in this order; cut from the bottom.

1. Auth, schema, product CRUD, **outbox dual-write** — without this nothing else demonstrates
2. Event tracking end to end, profile builder
3. **The LangGraph agent** with retrieval and grounding validation
4. Trigger policy and caching
5. Dashboard rendering with the transparency strip
6. README with architecture, metrics, and quickstart
7. *(bonus)* Scheduled digest email
8. *(bonus)* LangSmith tracing
9. *(bonus)* Eval harness
10. *(bonus)* Reranking, Telegram, deployed URL, demo video

**Never cut:** dual-write integrity, the LangGraph loop, grounding validation, Mesh routing.

---

## Definition of done

A component is done when it has: an Alembic migration if it touched the schema, a test covering its invariant, no hardcoded secrets, `python -m compileall` passing, and a line in the README if it is a judged feature. If a feature is a bonus item, the README must link to the exact file that implements it — a judge should never have to hunt.