"""Replay 3 divergent demo personas against a RUNNING SmartReco backend (PRD §9 "secret weapon";
IMPLEMENTATION.md Phase 9d). This is the "try it" step judges should run first: it drives the exact
same event -> profile -> trigger -> agent pipeline the frontend tracker drives, with **no manual
clicking**, so the dashboard's recommendation block visibly changes for three different learners in
about a minute.

What it does, per persona (Ana/Ben/Cara — the same seeded accounts as ``scripts/seed_data.py``):

  1. ``POST /api/auth/login``            — same credentialed session a browser tab would have.
  2. ``POST /api/events/batch``          — a wave of realistic behavior (searches, views, clicks,
     dwells, a cart add) concentrated in the persona's category, exactly like the tracker would send.
  3. ``POST /api/recommendations/refresh`` — force the trigger's full-run path *now*. The natural
     background trigger (``app/services/trigger.py``) has a 10-minute cooldown between generations, so
     it alone cannot show two visible changes inside a ~60s demo window; ``/refresh`` is the documented
     demo affordance for exactly this (rate-limited to once per 15s per user, never the "LLM on every
     event" antipattern — CLAUDE.md invariant #6 governs the *background* trigger, not an explicit
     user action).
  4. ``GET /api/recommendations/current`` — print the resulting headline/narrative/items.
  5. A second, divergent wave of events (a shift toward a related-but-different category) + another
     refresh, to show the recommendation block visibly change again for the *same* user within the run.

All three personas run **concurrently** (each with its own cookie session) so the whole replay — 2
waves x 3 personas — finishes in roughly 20-30 seconds, comfortably inside the "~60s" target.

Requires: the backend running (``uvicorn app.main:app``) and the catalog + demo users already seeded
(``python scripts/seed_data.py``). Safe to run against a ``MESH_DISABLED=true`` server (recommendations
still visibly change via the decayed interest centroid — no live Mesh call is required to see the
effect) or a live one (slower per refresh, capped at the agent's 25s budget).

Usage:
    # start the backend in one terminal:
    MESH_DISABLED=true SMARTRECO_RUN_SCHEDULER=true uvicorn app.main:app --reload
    # seed the catalog + demo users (once):
    MESH_DISABLED=true python scripts/seed_data.py
    # replay all 3 personas against it:
    python scripts/simulate_behavior.py
    python scripts/simulate_behavior.py --base-url http://localhost:8000 --only ana@smartreco.dev
    python scripts/simulate_behavior.py --waves 1          # one wave per persona, faster
    python scripts/simulate_behavior.py --password <pw>    # override SEED_DEMO_PASSWORD
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

try:
    import httpx
except ImportError:  # pragma: no cover - httpx is already in requirements.txt
    print(
        "This script needs `httpx` (already in requirements.txt). "
        "Run `pip install -r requirements.txt` first.",
        file=sys.stderr,
    )
    raise SystemExit(1)

DEFAULT_BASE_URL = "http://localhost:8000"
# Same default as scripts/seed_data.py's SEED_DEMO_PASSWORD env var — override with --password if the
# seed script was run with a custom SEED_DEMO_PASSWORD.
DEFAULT_DEMO_PASSWORD = os.getenv("SEED_DEMO_PASSWORD", "smartreco-demo-2026")

# POST /api/recommendations/refresh is rate-limited to one call per 15s per user
# (app/services/trigger.py REFRESH_RATE_LIMIT_SECONDS). A small buffer avoids a 429 at the boundary.
_REFRESH_RATE_LIMIT_SECONDS = 15
_REFRESH_GAP_SECONDS = 17.0
# How long to let the background bulk-insert + profile rebuild (app/api/routes/events.py's
# BackgroundTask) settle before forcing a refresh, so the agent run sees the just-posted behavior.
_SETTLE_SECONDS = 2.5
# /refresh can 409 if the natural background trigger (fired by the same event batch, since a brand-new
# user's first-ever generation has no cooldown to wait out) is already holding the gen:{user_id} lock —
# in that case the right move is to wait for *that* run to finish and read GET /current, not hammer
# /refresh again: the rate-limit check runs before the lock check, so every extra call just re-consumes
# the 15s window and guarantees the next few attempts 429 for no benefit. A 429 (the rate-limit window
# from an earlier call in this run hasn't expired) gets exactly one retry, after waiting it out.
_LOCK_WAIT_SECONDS = 3.0

_EVENT_TYPES_NEEDING_PRODUCT = {"view", "click", "dwell", "cart"}


# --------------------------------------------------------------------------------------------------
# Personas — same demo accounts scripts/seed_data.py creates, each given two divergent behavior waves
# so a single run shows both "cold start -> first recommendation" and "behavior shifts -> recs update".
# --------------------------------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class Wave:
    label: str
    categories: list[str]
    search_terms: list[str]


@dataclass(frozen=True, slots=True)
class Persona:
    email: str
    display_name: str
    waves: list[Wave]


PERSONAS: list[Persona] = [
    Persona(
        email="ana@smartreco.dev",
        display_name="Ana (Data/ML)",
        waves=[
            Wave(
                "data & ML fundamentals",
                ["Data Science", "Machine Learning"],
                ["python for data analysis", "machine learning fundamentals", "statistics foundations"],
            ),
            Wave(
                "shifts into applied AI/LLMs",
                ["AI & LLMs"],
                ["rag pipeline", "vector databases", "langgraph agents"],
            ),
        ],
    ),
    Persona(
        email="ben@smartreco.dev",
        display_name="Ben (Web Dev)",
        waves=[
            Wave(
                "frontend basics",
                ["Web Development"],
                ["javascript complete guide", "react hooks", "typescript generics"],
            ),
            Wave(
                "shifts into backend/API work",
                ["Web Development", "Programming"],
                ["fastapi rest api", "node express backend", "clean code refactoring"],
            ),
        ],
    ),
    Persona(
        email="cara@smartreco.dev",
        display_name="Cara (Cloud/Security)",
        waves=[
            Wave(
                "cloud & DevOps",
                ["Cloud & DevOps"],
                ["docker containers", "kubernetes for developers", "terraform iac"],
            ),
            Wave(
                "shifts into security",
                ["Cybersecurity"],
                ["web application security", "ethical hacking fundamentals"],
            ),
        ],
    ),
]


# --------------------------------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------------------------------
def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def _print(*parts: Any) -> None:
    # Unbuffered-ish so concurrent personas interleave readably in the terminal as they happen.
    print(*parts, flush=True)


async def fetch_catalog(client: httpx.AsyncClient) -> dict[str, list[dict[str, Any]]]:
    """``GET /api/products`` (public, unauthenticated) grouped by category, so waves can reference
    real ``product_id``s — never invented ids, mirroring the agent's own grounding discipline."""
    response = await client.get("/api/products", params={"page_size": 100})
    response.raise_for_status()
    by_category: dict[str, list[dict[str, Any]]] = {}
    for item in response.json()["items"]:
        by_category.setdefault(item["category"], []).append(item)
    return by_category


def _build_wave_events(
    session_id: uuid.UUID,
    wave: Wave,
    catalog: dict[str, list[dict[str, Any]]],
    start_ts: datetime,
) -> list[dict[str, Any]]:
    """A realistic, strictly-increasing-timestamp event stream for one wave: searches (high signal),
    then view -> click -> dwell -> cart on real in-focus products, mirroring the weighting
    scripts/seed_data.py bakes in (view 1.0 / click 1.5 / search 2.0 / dwell>30s 2.5 / cart 3.0) and
    guaranteeing at least one 'cart' (a high-intent event — the OR-arm the trigger checks on its own,
    independent of the 8-event threshold).
    """
    in_focus: list[dict[str, Any]] = []
    for category in wave.categories:
        in_focus.extend(catalog.get(category, []))
    if not in_focus:  # defensive — an unseeded/renamed category should never crash the replay
        in_focus = [p for products in catalog.values() for p in products][:6]

    events: list[dict[str, Any]] = []
    tick = 0

    def next_ts() -> datetime:
        nonlocal tick
        tick += 1
        # Unique, strictly increasing client_ts per event — required by the events natural key
        # UNIQUE(user_id, session_id, event_type, client_ts); mirrors the tracker's own beacon spacing.
        return start_ts + timedelta(milliseconds=tick * 250)

    def add(event_type: str, product: dict[str, Any] | None, payload: dict[str, Any] | None = None) -> None:
        events.append(
            {
                "session_id": str(session_id),
                "event_type": event_type,
                "client_ts": _iso(next_ts()),
                "product_id": product["id"] if product else None,
                "event_id": str(uuid.uuid4()),
                "payload": payload or {},
            }
        )

    for term in wave.search_terms:
        add("search", None, {"query": term})

    focus_slice = in_focus[:6]
    for index, product in enumerate(focus_slice):
        add("view", product)
        add("click", product)
        if index < 4:
            add("dwell", product, {"dwell_ms": 45_000})  # > 30s -> strong weight (2.5)
    for product in focus_slice[:2]:
        add("cart", product)  # guarantees the high-intent trigger arm fires on its own

    return events


# --------------------------------------------------------------------------------------------------
# HTTP steps against one persona's authenticated session
# --------------------------------------------------------------------------------------------------
async def _login(client: httpx.AsyncClient, email: str, password: str) -> bool:
    response = await client.post("/api/auth/login", json={"email": email, "password": password})
    if response.status_code == 200:
        return True
    _print(
        f"  [{email}] login failed ({response.status_code}): {response.text[:200]!r} — "
        "has `python scripts/seed_data.py` been run against this backend?"
    )
    return False


async def _post_events(client: httpx.AsyncClient, events: list[dict[str, Any]]) -> None:
    response = await client.post("/api/events/batch", json={"events": events})
    response.raise_for_status()


async def _refresh_or_wait(client: httpx.AsyncClient, label: str) -> dict[str, Any] | None:
    """``POST /refresh`` once. On ``409`` (someone else holds the ``gen:{user_id}`` lock — almost
    always the natural background trigger racing this same just-posted batch, since a brand-new user's
    first-ever generation has no cooldown to wait out) do **not** call ``/refresh`` again: the
    rate-limit check runs before the lock check, so a second call would just burn the 15s window and
    guarantee subsequent attempts 429 for nothing. Instead wait briefly for that other run to finish —
    the caller reads ``GET /current`` right after this returns. On ``429`` (this run's own rate-limit
    window from an earlier call hasn't expired) wait it out and retry exactly once.
    """
    response = await client.post("/api/recommendations/refresh")
    if response.status_code == 200:
        return response.json()
    if response.status_code == 409:
        _print(f"  [{label}] refresh: a generation is already running — waiting for it to finish...")
        await asyncio.sleep(_LOCK_WAIT_SECONDS)
        return None
    if response.status_code == 429:
        _print(f"  [{label}] refresh: rate-limited, waiting {_REFRESH_RATE_LIMIT_SECONDS + 1}s then retrying once...")
        await asyncio.sleep(_REFRESH_RATE_LIMIT_SECONDS + 1)
        retry = await client.post("/api/recommendations/refresh")
        if retry.status_code == 200:
            return retry.json()
        _print(f"  [{label}] refresh retry failed ({retry.status_code}): {retry.text[:200]!r}")
        return None
    _print(f"  [{label}] refresh failed ({response.status_code}): {response.text[:200]!r}")
    return None


async def _print_current(client: httpx.AsyncClient, label: str, wave_label: str) -> dict[str, Any] | None:
    response = await client.get("/api/recommendations/current")
    if response.status_code == 404:
        _print(f"  [{label}] {wave_label}: no recommendation yet.")
        return None
    response.raise_for_status()
    body = response.json()
    t = body["transparency"]
    _print(f"\n  [{label}] {wave_label}")
    _print(f"    headline   : {body['headline']}")
    narrative = body["narrative"].strip().replace("\n", " ")
    _print(f"    narrative  : {narrative[:160]}{'...' if len(narrative) > 160 else ''}")
    for item in body["items"]:
        _print(f"      - {item['title']} ({item['category']}) — {item['reason'][:90]}")
    _print(
        f"    based on {t['total_events']} actions · {t['searches']} searches · "
        f"trigger={body['trigger_reason']!r}"
    )
    return body


def _diff_note(before: dict[str, Any] | None, after: dict[str, Any] | None) -> str:
    """A `/refresh` forces a full run unconditionally (it does not go through the trigger gate in
    ``app/services/trigger.py`` at all), so if the items come back unchanged it is never because a
    threshold wasn't crossed — it is that retrieval/ranking landed on the same top candidates again.
    That is expected and honest, not a bug: the interest centroid is a *decayed* average over every
    event the user has ever sent (3-day half-life), so one short wave is one input among many and is
    not guaranteed to swing the top-3 by itself — especially offline, where MESH_DISABLED candidate
    vectors are seeded pseudo-random (see the reranker module docstring's honesty note) rather than
    real embeddings that would actually cluster by topic.
    """
    if before is None or after is None:
        return ""
    before_ids = [item["product_id"] for item in before.get("items", [])]
    after_ids = [item["product_id"] for item in after.get("items", [])]
    if before["headline"] != after["headline"] or before_ids != after_ids:
        return "    >>> recommendation changed since the last wave. <<<"
    return (
        "    (items unchanged this wave — retrieval landed on the same top candidates; the interest "
        "centroid moves gradually by design, and offline retrieval is noisier than live embeddings)"
    )


# --------------------------------------------------------------------------------------------------
# One persona's full replay (waves -> refresh -> print), run concurrently with the others
# --------------------------------------------------------------------------------------------------
async def run_persona(
    base_url: str,
    persona: Persona,
    password: str,
    catalog: dict[str, list[dict[str, Any]]],
    *,
    waves: int,
    settle_seconds: float,
    refresh_gap_seconds: float,
    timeout: httpx.Timeout,
) -> None:
    async with httpx.AsyncClient(base_url=base_url, timeout=timeout) as client:
        _print(f"[{persona.display_name}] logging in as {persona.email}...")
        if not await _login(client, persona.email, password):
            return

        previous: dict[str, Any] | None = None
        active_waves = persona.waves[:waves]
        for index, wave in enumerate(active_waves):
            session_id = uuid.uuid4()
            events = _build_wave_events(session_id, wave, catalog, _now())
            _print(f"[{persona.display_name}] wave {index + 1}/{len(active_waves)} — {wave.label}: sending {len(events)} events...")
            await _post_events(client, events)

            await asyncio.sleep(settle_seconds)
            result = await _refresh_or_wait(client, persona.display_name)
            if result is not None:
                _print(f"  [{persona.display_name}] refresh -> status={result.get('status')}")

            current = await _print_current(client, persona.display_name, f"after wave {index + 1} ({wave.label})")
            if index > 0:
                _print(_diff_note(previous, current))
            previous = current

            is_last_wave = index == len(active_waves) - 1
            if not is_last_wave:
                await asyncio.sleep(refresh_gap_seconds)

        _print(f"[{persona.display_name}] done.\n")


# --------------------------------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------------------------------
async def _check_server_up(base_url: str, timeout: httpx.Timeout) -> bool:
    try:
        async with httpx.AsyncClient(base_url=base_url, timeout=timeout) as client:
            response = await client.get("/health")
            return response.status_code < 500
    except (httpx.ConnectError, httpx.ConnectTimeout):
        return False
    except httpx.HTTPError:
        # Any non-connection HTTP error still means *something* is listening on base_url.
        return True


async def _run(args: argparse.Namespace) -> int:
    timeout = httpx.Timeout(connect=5.0, read=40.0, write=10.0, pool=5.0)

    if not await _check_server_up(args.base_url, timeout):
        _print(
            f"Could not reach {args.base_url} — is the backend running?\n"
            f"  MESH_DISABLED=true uvicorn app.main:app --reload\n"
            f"and has the catalog been seeded?\n"
            f"  MESH_DISABLED=true python scripts/seed_data.py"
        )
        return 1

    personas = [p for p in PERSONAS if args.only is None or p.email == args.only]
    if not personas:
        _print(f"No persona matches --only {args.only!r}. Known: {[p.email for p in PERSONAS]}")
        return 1

    async with httpx.AsyncClient(base_url=args.base_url, timeout=timeout) as client:
        try:
            catalog = await fetch_catalog(client)
        except httpx.HTTPError as exc:
            _print(f"Could not fetch the product catalog from {args.base_url}: {exc}")
            return 1
    if not catalog:
        _print(
            "Catalog is empty — has `python scripts/seed_data.py` been run against this backend?"
        )
        return 1

    _print(
        f"Replaying {len(personas)} persona(s) x {args.waves} wave(s) against {args.base_url} "
        f"({sum(len(v) for v in catalog.values())} products across {len(catalog)} categories)...\n"
    )
    start = time.monotonic()
    await asyncio.gather(
        *(
            run_persona(
                args.base_url,
                persona,
                args.password,
                catalog,
                waves=args.waves,
                settle_seconds=args.settle_seconds,
                refresh_gap_seconds=args.refresh_gap,
                timeout=timeout,
            )
            for persona in personas
        )
    )
    elapsed = time.monotonic() - start
    _print(f"All done in {elapsed:.1f}s. Open the dashboard to see the same recommendations rendered.")
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL, help=f"default: {DEFAULT_BASE_URL}")
    parser.add_argument(
        "--password", default=DEFAULT_DEMO_PASSWORD, help="demo account password (SEED_DEMO_PASSWORD)"
    )
    parser.add_argument(
        "--only", default=None, help="replay a single persona by email instead of all 3"
    )
    parser.add_argument(
        "--waves", type=int, default=2, choices=(1, 2), help="behavior waves per persona (default: 2)"
    )
    parser.add_argument(
        "--settle-seconds",
        type=float,
        default=_SETTLE_SECONDS,
        help="pause after posting events, before forcing a refresh (let the background ingest settle)",
    )
    parser.add_argument(
        "--refresh-gap",
        type=float,
        default=_REFRESH_GAP_SECONDS,
        help=f"pause between a persona's waves (must exceed the {_REFRESH_RATE_LIMIT_SECONDS}s /refresh rate limit)",
    )
    args = parser.parse_args()
    exit_code = asyncio.run(_run(args))
    raise SystemExit(exit_code)


if __name__ == "__main__":
    main()
