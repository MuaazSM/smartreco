"""Tracker load test (PRD §6.3 "Prove it"; IMPLEMENTATION.md Phase 5).

Fires **1,000 events** through the tracker's real hot path and asserts **p95 client-handler time
< 2 ms** with **zero dropped events**. This is the "nobody else will do this" proof the PRD calls out
and a README highlight.

Rather than a re-implementation, the harness (`frontend/tests/trackerLoad.mjs`) imports the exact
`trackerCore` the Web Worker runs, so the number measured is the production ring-buffer hot path. We
drive it through Node (the frontend's own runtime) — no browser install required, which keeps this
runnable in CI. If Node is unavailable or too old for native TypeScript execution, the test skips with
a clear reason instead of failing (the harness is still runnable by hand:
``node frontend/tests/trackerLoad.mjs``).

A Playwright/Chromium variant would additionally exercise the DOM plumbing (scroll throttle, dwell
IntersectionObserver, sendBeacon), but the p95/zero-drop claim is about the buffered hot path this
harness measures directly.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
_HARNESS = _REPO_ROOT / "frontend" / "tests" / "trackerLoad.mjs"

_P95_BUDGET_MS = 2.0
_EVENT_COUNT = 1000


def _node_major(node: str) -> int:
    try:
        out = subprocess.run([node, "--version"], capture_output=True, text=True, timeout=15)
    except Exception:  # noqa: BLE001
        return 0
    match = re.search(r"v(\d+)", out.stdout.strip())
    return int(match.group(1)) if match else 0


def _run_harness(node: str, count: int) -> dict:
    """Run the Node harness, retrying with the type-strip flag on older Node, and parse its JSON."""
    base_cmd = [node, str(_HARNESS), str(count)]
    attempts = [base_cmd, [node, "--experimental-strip-types", str(_HARNESS), str(count)]]
    last_err = ""
    for cmd in attempts:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        if result.returncode == 0 and result.stdout.strip():
            # Take the last JSON object line (Node may print warnings to stdout on some versions).
            for line in reversed(result.stdout.strip().splitlines()):
                line = line.strip()
                if line.startswith("{") and line.endswith("}"):
                    return json.loads(line)
        last_err = (result.stderr or result.stdout).strip()
    raise RuntimeError(f"harness did not emit parseable JSON: {last_err[:500]}")


@pytest.mark.skipif(not _HARNESS.exists(), reason="tracker load harness not found")
def test_tracker_handles_1000_events_under_p95_budget_with_zero_drops() -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node runtime not available — run `node frontend/tests/trackerLoad.mjs` manually")
    if _node_major(node) < 22:
        pytest.skip("node >= 22 required for native TypeScript execution of the harness")

    metrics = _run_harness(node, _EVENT_COUNT)

    assert metrics["fired"] == _EVENT_COUNT
    # Zero dropped events: the flush cadence keeps the ring buffer far below its 200-item cap.
    assert metrics["dropped"] == 0, metrics
    # Lossless: every fired event was captured/flushed, none lost.
    assert metrics["sent"] == _EVENT_COUNT, metrics
    # The headline claim: p95 per-event handler time under 2 ms.
    assert metrics["p95_ms"] < _P95_BUDGET_MS, metrics
