/**
 * Tracker load harness (PRD §6.3 "Prove it") — drives the REAL production hot path.
 *
 * It imports the same `trackerCore` the Web Worker uses (no re-implementation) and fires 1,000
 * events through the exact per-event sequence the worker runs synchronously: push to the ring buffer,
 * evaluate the flush policy, and — every 20 events — drain + serialize the batch (the sync work that
 * precedes the async network send, which is off-thread and therefore excluded).
 *
 * Emits a single JSON line: {fired, sent, dropped, p95_ms, max_ms, mean_ms}. `tests/test_tracker_load.py`
 * parses it and asserts p95 < 2 ms with zero dropped events.
 *
 * Run directly:  node frontend/tests/trackerLoad.mjs [N]
 */

import { RingBuffer, shouldFlush } from "../lib/trackerCore.ts";

const N = Number(process.argv[2] ?? 1000);

const buffer = new RingBuffer();
let lastFlushMs = Date.now();
let sent = 0;
const times = new Array(N);

for (let i = 0; i < N; i++) {
  const event = {
    event_id: `evt-${i}`,
    session_id: "load-test-session",
    event_type: "view",
    client_ts: new Date().toISOString(),
    product_id: null,
    payload: {},
  };

  const t0 = performance.now();
  // ---- exact worker hot path for one tracked event ----
  buffer.push(event);
  if (shouldFlush(buffer.size, lastFlushMs, Date.now())) {
    const batch = buffer.drain();
    // The synchronous serialization the worker does before handing off to fetch/sendBeacon.
    const body = JSON.stringify({ events: batch });
    if (body.length > 0) sent += batch.length;
    lastFlushMs = Date.now();
  }
  const t1 = performance.now();

  times[i] = t1 - t0;
}

// Final flush of the tail so `sent` accounts for every event.
sent += buffer.drain().length;

times.sort((a, b) => a - b);
const p95 = times[Math.min(times.length - 1, Math.floor(times.length * 0.95))];
const max = times[times.length - 1];
const mean = times.reduce((acc, v) => acc + v, 0) / times.length;

process.stdout.write(
  JSON.stringify({
    fired: N,
    sent,
    dropped: buffer.dropped,
    p95_ms: Number(p95.toFixed(4)),
    max_ms: Number(max.toFixed(4)),
    mean_ms: Number(mean.toFixed(4)),
  }) + "\n",
);
