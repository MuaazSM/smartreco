"use client";
/**
 * Behavioral tracker — main-thread public API + DOM wiring (PRD §6.3 + Figure 4).
 *
 * The heavy lifting (ring buffer, batching, network flush) lives in the Web Worker
 * (`tracker.worker.ts`), so nothing here blocks the main thread. This module only:
 *   - mints a per-tab `session_id` and a client UUID per event (idempotency);
 *   - collapses high-frequency signals before they ever reach the worker — **scroll throttled to one
 *     per 500 ms** and reduced to milestone depths 25/50/75/100, **search debounced 400 ms**;
 *   - measures **dwell** via `IntersectionObserver`, accumulating **foreground time only**
 *     (`visibilitychange`), and emits exactly one dwell event on unmount;
 *   - fires a **lifecycle flush** on `visibilitychange → hidden` / `pagehide` (the worker uses
 *     `navigator.sendBeacon`, which survives tab close where `fetch` dies);
 *   - persists a **failed batch to `localStorage`** (only the main thread can) and replays it on next
 *     load.
 *
 * No `weight` is ever sent — the server assigns it (PRD §6.3 "Trust"). No synchronous XHR anywhere.
 *
 * Scroll milestones are emitted as `view` events carrying `payload.scroll_depth` (the server accepts
 * the five canonical types view/click/search/dwell/cart): a page-level scroll milestone is a mild
 * engagement signal, so it registers activity without inventing a sixth event type.
 */

import {
  parseSpillover,
  serializeSpillover,
  SPILLOVER_KEY,
  type TrackedEvent,
} from "./trackerCore";

const SCROLL_THROTTLE_MS = 500;
const SEARCH_DEBOUNCE_MS = 400;
const SCROLL_MILESTONES = [25, 50, 75, 100] as const;
const SESSION_KEY = "smartreco:tracker:session";

export interface TrackerInit {
  endpoint?: string;
}

export interface TrackOptions {
  productId?: string | null;
  payload?: Record<string, unknown>;
}

type WorkerOutbound =
  | { type: "config"; endpoint: string }
  | { type: "track"; event: TrackedEvent }
  | { type: "replay"; events: TrackedEvent[] }
  | { type: "flush"; reason: "lifecycle" };

class Tracker {
  private worker: Worker | null = null;
  private sessionId = "";
  private endpoint = "/api/events/batch";
  private started = false;

  private searchTimer: ReturnType<typeof setTimeout> | null = null;
  private scrollThrottleUntil = 0;
  private firedMilestones = new Set<number>();
  private disposers: Array<() => void> = [];

  /** Boot the tracker: spawn the worker, replay any spillover, wire lifecycle + scroll. Idempotent. */
  init(opts: TrackerInit = {}): void {
    if (this.started || typeof window === "undefined") return;
    this.started = true;
    this.endpoint = opts.endpoint ?? this.endpoint;
    this.sessionId = this.ensureSessionId();

    this.worker = new Worker(new URL("./tracker.worker.ts", import.meta.url));
    this.worker.onmessage = (event: MessageEvent) => this.onWorkerMessage(event);
    this.send({ type: "config", endpoint: this.endpoint });

    this.replaySpillover();
    this.wireLifecycle();
    this.enableScrollTracking();
  }

  /** Low-level: enqueue one event of any canonical type. */
  track(eventType: string, options: TrackOptions = {}): void {
    if (!this.worker) return;
    this.send({ type: "track", event: this.makeEvent(eventType, options) });
  }

  trackView(productId?: string, payload?: Record<string, unknown>): void {
    this.track("view", { productId, payload });
  }

  trackClick(productId?: string, payload?: Record<string, unknown>): void {
    this.track("click", { productId, payload });
  }

  trackCart(productId: string, payload?: Record<string, unknown>): void {
    this.track("cart", { productId, payload });
  }

  /** Search is debounced 400 ms so individual keystrokes don't each become an event (PRD §6.3). */
  trackSearch(query: string): void {
    if (this.searchTimer) clearTimeout(this.searchTimer);
    const trimmed = query.trim();
    this.searchTimer = setTimeout(() => {
      if (trimmed) this.track("search", { payload: { query: trimmed } });
    }, SEARCH_DEBOUNCE_MS);
  }

  /**
   * Observe an element's dwell time. Accumulates foreground visible time only (paused when the
   * element scrolls out of view or the tab is hidden) and emits exactly one `dwell` event when the
   * returned disposer runs (component unmount). `dwell_ms` lets the server apply the >30 s → 2.5 rule.
   */
  observeDwell(element: Element, productId: string): () => void {
    if (typeof window === "undefined" || !("IntersectionObserver" in window)) {
      return () => undefined;
    }
    let accumulatedMs = 0;
    let visibleSince: number | null = null;

    const foreground = (): boolean => document.visibilityState === "visible";
    const resume = (): void => {
      if (visibleSince === null && foreground()) visibleSince = performance.now();
    };
    const pause = (): void => {
      if (visibleSince !== null) {
        accumulatedMs += performance.now() - visibleSince;
        visibleSince = null;
      }
    };

    let intersecting = false;
    const observer = new IntersectionObserver(
      (entries) => {
        for (const entry of entries) {
          intersecting = entry.isIntersecting;
          if (intersecting) resume();
          else pause();
        }
      },
      { threshold: 0.5 },
    );
    observer.observe(element);

    const onVisibility = (): void => {
      if (foreground() && intersecting) resume();
      else pause();
    };
    document.addEventListener("visibilitychange", onVisibility);

    const dispose = (): void => {
      pause();
      observer.disconnect();
      document.removeEventListener("visibilitychange", onVisibility);
      if (accumulatedMs > 0) {
        this.track("dwell", {
          productId,
          payload: { dwell_ms: Math.round(accumulatedMs) },
        });
      }
    };
    this.disposers.push(dispose);
    return dispose;
  }

  /** Tear everything down (e.g. on logout). Finalizes dwell observers and flushes. */
  stop(): void {
    for (const dispose of this.disposers.splice(0)) dispose();
    this.flushLifecycle();
    if (this.searchTimer) clearTimeout(this.searchTimer);
  }

  // ---- internals -------------------------------------------------------------------------------

  private makeEvent(eventType: string, options: TrackOptions): TrackedEvent {
    return {
      event_id: this.uuid(),
      session_id: this.sessionId,
      event_type: eventType,
      client_ts: new Date().toISOString(),
      product_id: options.productId ?? null,
      payload: options.payload ?? {},
    };
  }

  private uuid(): string {
    if (typeof crypto !== "undefined" && "randomUUID" in crypto) {
      return crypto.randomUUID();
    }
    // Fallback for the rare environment without crypto.randomUUID.
    return `${Date.now()}-${Math.random().toString(16).slice(2)}-${Math.random()
      .toString(16)
      .slice(2)}`;
  }

  private ensureSessionId(): string {
    try {
      const existing = window.sessionStorage.getItem(SESSION_KEY);
      if (existing) return existing;
      const fresh = this.uuid();
      window.sessionStorage.setItem(SESSION_KEY, fresh);
      return fresh;
    } catch {
      return this.uuid();
    }
  }

  private send(message: WorkerOutbound): void {
    this.worker?.postMessage(message);
  }

  private onWorkerMessage(event: MessageEvent): void {
    const message = event.data as { type?: string; events?: TrackedEvent[] };
    if (message?.type === "spillover" && message.events) {
      this.persistSpillover(message.events);
    }
  }

  private persistSpillover(events: TrackedEvent[]): void {
    try {
      const existing = parseSpillover(window.localStorage.getItem(SPILLOVER_KEY));
      window.localStorage.setItem(
        SPILLOVER_KEY,
        serializeSpillover([...existing, ...events]),
      );
    } catch {
      // localStorage may be unavailable (private mode / quota) — dropping spillover is acceptable.
    }
  }

  private replaySpillover(): void {
    try {
      const events = parseSpillover(window.localStorage.getItem(SPILLOVER_KEY));
      if (events.length > 0) {
        window.localStorage.removeItem(SPILLOVER_KEY);
        this.send({ type: "replay", events });
      }
    } catch {
      // No spillover to replay.
    }
  }

  private wireLifecycle(): void {
    const onHidden = (): void => {
      if (document.visibilityState === "hidden") this.flushLifecycle();
    };
    document.addEventListener("visibilitychange", onHidden);
    window.addEventListener("pagehide", () => this.flushLifecycle());
  }

  private flushLifecycle(): void {
    this.send({ type: "flush", reason: "lifecycle" });
  }

  private enableScrollTracking(): void {
    const onScroll = (): void => {
      const now = Date.now();
      if (now < this.scrollThrottleUntil) return; // throttle: at most one handler per 500 ms
      this.scrollThrottleUntil = now + SCROLL_THROTTLE_MS;

      const depth = this.scrollDepthPercent();
      for (const milestone of SCROLL_MILESTONES) {
        if (depth >= milestone && !this.firedMilestones.has(milestone)) {
          this.firedMilestones.add(milestone);
          // Emitted as a canonical `view` with a scroll_depth marker (see module docstring).
          this.track("view", { payload: { scroll_depth: milestone } });
        }
      }
    };
    window.addEventListener("scroll", onScroll, { passive: true });
  }

  private scrollDepthPercent(): number {
    const doc = document.documentElement;
    const scrollable = doc.scrollHeight - doc.clientHeight;
    if (scrollable <= 0) return 100;
    return Math.min(100, Math.round(((window.scrollY || doc.scrollTop) / scrollable) * 100));
  }
}

/** Process-wide singleton. Call `tracker.init()` once from a top-level client component. */
export const tracker = new Tracker();
