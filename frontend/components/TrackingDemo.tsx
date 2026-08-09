"use client";
/**
 * A small live demonstration of the tracker's public API (Phase 5). Phase 8 builds the real catalog /
 * dashboard UI; this exists so the scaffold visibly exercises search-debounce, click/cart, and
 * IntersectionObserver dwell against the running tracker. Events batch to `/api/events/batch` in the
 * background — with the backend down they spill to localStorage and replay on next load.
 */

import { useEffect, useRef, useState } from "react";

import { tracker } from "../lib/tracker";

const DEMO_PRODUCT_ID = "00000000-0000-0000-0000-000000000000";

export function TrackingDemo(): React.ReactElement {
  const [query, setQuery] = useState("");
  const cardRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    const el = cardRef.current;
    if (!el) return;
    // One dwell event is emitted when this component unmounts, carrying accumulated foreground time.
    const dispose = tracker.observeDwell(el, DEMO_PRODUCT_ID);
    return dispose;
  }, []);

  return (
    <section className="mt-10 rounded-xl border border-neutral-200 p-6 dark:border-neutral-800">
      <h2 className="text-lg font-semibold">Tracker demo</h2>
      <p className="mt-1 text-sm text-neutral-500 dark:text-neutral-400">
        Every interaction below is throttled/debounced, buffered off the main thread, and batched.
      </p>

      <label className="mt-4 block text-sm font-medium">Search (debounced 400 ms)</label>
      <input
        className="mt-1 w-full rounded-md border border-neutral-300 bg-transparent px-3 py-2 text-sm dark:border-neutral-700"
        placeholder="e.g. deep learning pytorch"
        value={query}
        onChange={(e) => {
          setQuery(e.target.value);
          tracker.trackSearch(e.target.value);
        }}
      />

      <div
        ref={cardRef}
        className="mt-4 rounded-lg border border-neutral-200 p-4 dark:border-neutral-800"
      >
        <p className="font-medium">Sample course card (dwell-observed)</p>
        <div className="mt-3 flex gap-2">
          <button
            className="rounded-md bg-neutral-900 px-3 py-1.5 text-sm text-white dark:bg-white dark:text-neutral-900"
            onClick={() => tracker.trackClick(DEMO_PRODUCT_ID)}
          >
            Click
          </button>
          <button
            className="rounded-md border border-neutral-300 px-3 py-1.5 text-sm dark:border-neutral-700"
            onClick={() => tracker.trackCart(DEMO_PRODUCT_ID)}
          >
            Add to cart
          </button>
        </div>
      </div>
    </section>
  );
}
