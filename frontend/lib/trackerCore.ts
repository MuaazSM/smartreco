/**
 * Tracker hot-path core (PRD §6.3 + Figure 4) — the performance-critical, dependency-free heart of
 * the behavioral tracker.
 *
 * This module has **no DOM and no Web Worker dependency** on purpose: it is imported both by the
 * real Web Worker (`tracker.worker.ts`) and, unchanged, by the load test harness
 * (`trackerLoad.harness.ts` → `tests/test_tracker_load.py`). Measuring the exact code that runs in
 * production is what makes the "p95 client-handler time < 2 ms, zero dropped events over 1,000
 * events" claim honest rather than a toy re-implementation.
 *
 * Design points enforced here:
 *   - ring buffer **capped at 200, drop-oldest** (never unbounded memory, never blocks);
 *   - flush policy **20 events OR 10 s, whichever first**;
 *   - a failed flush **returns its events to the buffer** (oldest-first) so nothing is silently lost
 *     until it genuinely overflows the cap.
 *
 * `weight` is deliberately absent from `TrackedEvent` — the server assigns it (PRD §6.3 "Trust").
 */

export interface TrackedEvent {
  /** Client-generated UUID; rides along for idempotency traceability (server dedupes on natural key). */
  event_id: string;
  session_id: string;
  /** One of: view | click | search | dwell | cart. Kept as string so the core stays policy-free. */
  event_type: string;
  /** ISO-8601 timestamp; part of the server-side natural key. */
  client_ts: string;
  product_id?: string | null;
  payload?: Record<string, unknown>;
}

export const RING_BUFFER_CAP = 200;
export const FLUSH_AT_EVENTS = 20;
export const FLUSH_INTERVAL_MS = 10_000;

export interface FlushPolicy {
  flushAt: number;
  intervalMs: number;
}

export const DEFAULT_FLUSH_POLICY: FlushPolicy = {
  flushAt: FLUSH_AT_EVENTS,
  intervalMs: FLUSH_INTERVAL_MS,
};

/**
 * A fixed-capacity FIFO with drop-oldest semantics and a dropped-event counter. Pushing while at
 * capacity evicts the oldest entry (and increments `dropped`) — memory is bounded no matter how fast
 * events arrive. The normal flush cadence keeps `size` far below the cap, so `dropped` stays 0.
 */
export class RingBuffer<T> {
  private items: T[] = [];
  private readonly cap: number;
  private droppedCount = 0;

  constructor(cap: number = RING_BUFFER_CAP) {
    this.cap = cap;
  }

  get size(): number {
    return this.items.length;
  }

  get dropped(): number {
    return this.droppedCount;
  }

  /** O(1) amortized while below capacity; evicts the oldest (O(n) shift) only once full. */
  push(item: T): void {
    if (this.items.length >= this.cap) {
      this.items.shift();
      this.droppedCount += 1;
    }
    this.items.push(item);
  }

  /** Take everything currently buffered, leaving the buffer empty. */
  drain(): T[] {
    const out = this.items;
    this.items = [];
    return out;
  }

  /** Non-destructive copy (used to serialize for spillover without emptying the buffer). */
  snapshot(): T[] {
    return this.items.slice();
  }

  /** Return failed-flush events to the front (oldest-first), dropping oldest beyond the cap. */
  requeueFront(items: T[]): void {
    if (items.length === 0) return;
    this.items = items.concat(this.items);
    while (this.items.length > this.cap) {
      this.items.shift();
      this.droppedCount += 1;
    }
  }
}

/**
 * Whether the buffer should be flushed now: never on empty, immediately at/above the event
 * threshold, otherwise once the time-since-last-flush interval has elapsed.
 */
export function shouldFlush(
  size: number,
  lastFlushMs: number,
  nowMs: number,
  policy: FlushPolicy = DEFAULT_FLUSH_POLICY,
): boolean {
  if (size <= 0) return false;
  if (size >= policy.flushAt) return true;
  return nowMs - lastFlushMs >= policy.intervalMs;
}

/** localStorage key under which the main thread persists failed batches for next-load replay. */
export const SPILLOVER_KEY = "smartreco:tracker:spillover";

/** Serialize a spillover batch (cap defends against an unbounded localStorage entry). */
export function serializeSpillover(
  events: TrackedEvent[],
  cap: number = RING_BUFFER_CAP,
): string {
  const trimmed = events.length > cap ? events.slice(events.length - cap) : events;
  return JSON.stringify(trimmed);
}

/** Parse a spillover blob back into events; returns [] on absent/corrupt data (never throws). */
export function parseSpillover(raw: string | null): TrackedEvent[] {
  if (!raw) return [];
  try {
    const parsed = JSON.parse(raw);
    return Array.isArray(parsed) ? (parsed as TrackedEvent[]) : [];
  } catch {
    return [];
  }
}
