"use client";
/**
 * One recommended product card: title/category/price, the grounded "why this" reason line, and
 * thumbs up/down feedback (PRD §6.6). Feedback posts to `POST /api/recommendations/feedback`, which
 * writes a `feedback` event that feeds the *next* trigger cycle — see `app/api/routes/recommendations.py`.
 */

import { useState } from "react";

import { apiPost, ApiError } from "../../lib/api";
import { tracker } from "../../lib/tracker";
import type { FeedbackSignal, RecommendationItem } from "../../lib/types";

function formatPrice(cents: number): string {
  if (cents <= 0) return "Free";
  return `$${(cents / 100).toFixed(2)}`;
}

export function RecommendationCard({
  item,
  recId,
}: {
  item: RecommendationItem;
  recId: string;
}): React.ReactElement {
  const [signal, setSignal] = useState<FeedbackSignal | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function sendFeedback(next: FeedbackSignal): Promise<void> {
    if (submitting) return;
    const previous = signal;
    setSignal(next); // optimistic — never block the UI on the network round trip
    setSubmitting(true);
    setError(null);
    try {
      await apiPost("/api/recommendations/feedback", {
        rec_id: recId,
        item_id: item.product_id,
        signal: next,
      });
      tracker.trackClick(item.product_id, { source: "dashboard_feedback", signal: next });
    } catch (err) {
      setSignal(previous);
      setError(err instanceof ApiError ? err.message : "Could not record feedback");
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <article className="group flex flex-col rounded-2xl border border-neutral-200 bg-white p-5 shadow-sm transition-all duration-200 hover:-translate-y-0.5 hover:border-neutral-300 hover:shadow-lg dark:border-neutral-800 dark:bg-neutral-900 dark:hover:border-neutral-700">
      {/* Header: category eyebrow + price, then the title */}
      <div className="flex items-start justify-between gap-3">
        <p className="font-mono text-xs uppercase tracking-wider text-neutral-500 dark:text-neutral-400">
          {item.category || "course"}
        </p>
        <span className="shrink-0 whitespace-nowrap font-mono text-sm font-semibold text-neutral-900 dark:text-neutral-100">
          {formatPrice(item.price_cents)}
        </span>
      </div>

      <h3 className="mt-2 line-clamp-2 text-lg font-semibold leading-snug">
        {item.title || item.product_id}
      </h3>

      {item.level && (
        <span className="mt-2 inline-flex w-fit items-center rounded-full bg-neutral-100 px-2.5 py-0.5 font-mono text-xs text-neutral-600 dark:bg-neutral-800 dark:text-neutral-300">
          {item.level}
        </span>
      )}

      {/* The grounded reason — the point of the whole product, so give it real presence. */}
      <div className="mt-4 flex-1 rounded-xl border-l-2 border-accent bg-accent-soft px-4 py-3">
        <p className="font-mono text-xs font-semibold uppercase tracking-wider text-accent">
          Why this
        </p>
        <p className="mt-1.5 text-sm leading-relaxed text-neutral-700 dark:text-neutral-200">
          {item.reason}
        </p>
      </div>

      {/* Footer: link out + feedback */}
      <div className="mt-4 flex items-center justify-between">
        <a
          href={`/catalog/${item.product_id}`}
          className="group/link inline-flex items-center gap-1 text-sm font-medium text-accent"
          onClick={() => tracker.trackClick(item.product_id, { source: "dashboard_card" })}
        >
          View course
          <span aria-hidden className="transition-transform duration-150 group-hover/link:translate-x-0.5">
            →
          </span>
        </a>
        <div className="flex items-center gap-1.5" role="group" aria-label="Feedback on this recommendation">
          <button
            type="button"
            aria-label="Recommend more like this"
            aria-pressed={signal === "up"}
            disabled={submitting}
            onClick={() => sendFeedback("up")}
            className={`rounded-lg border px-3 py-1.5 text-sm transition-colors ${
              signal === "up"
                ? "border-emerald-500 bg-emerald-50 text-emerald-700 dark:border-emerald-500 dark:bg-emerald-950 dark:text-emerald-300"
                : "border-neutral-200 hover:bg-neutral-100 dark:border-neutral-700 dark:hover:bg-neutral-800"
            }`}
          >
            👍
          </button>
          <button
            type="button"
            aria-label="Show me less like this"
            aria-pressed={signal === "down"}
            disabled={submitting}
            onClick={() => sendFeedback("down")}
            className={`rounded-lg border px-3 py-1.5 text-sm transition-colors ${
              signal === "down"
                ? "border-red-500 bg-red-50 text-red-700 dark:border-red-500 dark:bg-red-950 dark:text-red-300"
                : "border-neutral-200 hover:bg-neutral-100 dark:border-neutral-700 dark:hover:bg-neutral-800"
            }`}
          >
            👎
          </button>
        </div>
      </div>
      {error && <p className="mt-2 text-xs text-red-600 dark:text-red-400">{error}</p>}
    </article>
  );
}
