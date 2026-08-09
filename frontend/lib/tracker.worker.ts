/// <reference lib="webworker" />
/**
 * The tracker's Web Worker (PRD §6.3 "Never block the UI"). All queue work — the ring buffer, the
 * flush timers, and the network I/O — lives here, off the main thread, so the page's interaction
 * handlers stay under the p95 budget no matter how many events arrive.
 *
 * The buffer logic is the shared `trackerCore` (the same code the load test measures). This file adds
 * only the browser plumbing the core deliberately omits:
 *   - a 1 s tick that enforces the 10 s time-based flush;
 *   - a `fetch(keepalive)` flush during normal operation and `navigator.sendBeacon` on a lifecycle
 *     flush (tab close / hidden), which survives navigation where `fetch` is killed;
 *   - on any flush failure, the batch is returned to the buffer AND posted back to the main thread as
 *     `spillover` (only the main thread can touch `localStorage`).
 *
 * No synchronous XHR, ever. `weight` is never set here — the server assigns it.
 */

import {
  DEFAULT_FLUSH_POLICY,
  RingBuffer,
  shouldFlush,
  type TrackedEvent,
} from "./trackerCore";

declare const self: DedicatedWorkerGlobalScope;

type InboundMessage =
  | { type: "config"; endpoint: string }
  | { type: "track"; event: TrackedEvent }
  | { type: "replay"; events: TrackedEvent[] }
  | { type: "flush"; reason: FlushReason };

type FlushReason = "threshold" | "interval" | "lifecycle";

const buffer = new RingBuffer<TrackedEvent>();
let endpoint = "/api/events/batch";
let lastFlushMs = Date.now();
let flushing = false;

// Enforce the time-based arm of the flush policy (20 events OR 10 s). The threshold arm is checked
// synchronously on every `track`, so this tick only needs to cover the idle-but-nonempty case.
self.setInterval(() => {
  void maybeFlush("interval");
}, 1000);

self.onmessage = (event: MessageEvent<InboundMessage>) => {
  const message = event.data;
  switch (message.type) {
    case "config":
      endpoint = message.endpoint;
      break;
    case "track":
      buffer.push(message.event);
      if (shouldFlush(buffer.size, lastFlushMs, Date.now())) {
        void flush("threshold");
      }
      break;
    case "replay":
      // Spillover from a previous load, replayed by the main thread. Re-enqueue and flush eagerly.
      for (const e of message.events) buffer.push(e);
      void maybeFlush("interval");
      break;
    case "flush":
      void flush(message.reason);
      break;
  }
};

async function maybeFlush(reason: FlushReason): Promise<void> {
  if (shouldFlush(buffer.size, lastFlushMs, Date.now())) {
    await flush(reason);
  }
}

async function flush(reason: FlushReason): Promise<void> {
  // A lifecycle flush must go out even mid-flight (the page may be closing); other reasons coalesce.
  if (flushing && reason !== "lifecycle") return;
  const batch = buffer.drain();
  if (batch.length === 0) return;

  flushing = true;
  lastFlushMs = Date.now();
  const body = JSON.stringify({ events: batch });

  try {
    const ok =
      reason === "lifecycle" ? sendViaBeacon(body) : await sendViaFetch(body);
    if (!ok) throw new Error("flush transport reported failure");
    self.postMessage({ type: "flushed", count: batch.length });
  } catch {
    // Return the batch to the buffer AND hand it to the main thread for localStorage spillover.
    buffer.requeueFront(batch);
    self.postMessage({ type: "spillover", events: batch });
  } finally {
    flushing = false;
  }
}

// `sendBeacon` exists on a dedicated worker's navigator at runtime, but TS's `WorkerNavigator` lib
// type omits it — feature-detect through a narrow cast rather than widening the whole lib.
type BeaconNavigator = { sendBeacon?: (url: string, data?: BodyInit | null) => boolean };

function sendViaBeacon(body: string): boolean {
  const nav = self.navigator as unknown as BeaconNavigator | undefined;
  if (nav && typeof nav.sendBeacon === "function") {
    const blob = new Blob([body], { type: "application/json" });
    return nav.sendBeacon(endpoint, blob);
  }
  return false;
}

async function sendViaFetch(body: string): Promise<boolean> {
  const response = await fetch(endpoint, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body,
    keepalive: true, // let an in-flight flush survive a same-tab navigation
    credentials: "include", // carry the httpOnly auth cookie
  });
  return response.ok;
}

// Ensure the flush policy constants are treated as used even if tree-shaking is aggressive.
export const _flushPolicy = DEFAULT_FLUSH_POLICY;
