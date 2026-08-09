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
    <article className="flex flex-col rounded-xl border border-neutral-200 p-5 dark:border-neutral-800">
      <div className="flex items-start justify-between gap-3">
        <div>
          <h3 className="font-semibold leading-snug">{item.title || item.product_id}</h3>
          <p className="mt-0.5 text-xs uppercase tracking-wide text-neutral-500">
            {item.category || "course"} · {item.level || "any level"}
          </p>
        </div>
        <span className="whitespace-nowrap text-sm font-medium text-neutral-700 dark:text-neutral-300">
          {formatPrice(item.price_cents)}
        </span>
      </div>

      <p className="mt-3 flex-1 text-sm text-neutral-600 dark:text-neutral-400">
        <span className="font-medium text-neutral-800 dark:text-neutral-200">Why this: </span>
        {item.reason}
      </p>

      <div className="mt-4 flex items-center justify-between">
        <a
          href={`/catalog/${item.product_id}`}
          className="text-sm font-medium underline underline-offset-2"
          onClick={() => tracker.trackClick(item.product_id, { source: "dashboard_card" })}
        >
          View course
        </a>
        <div className="flex items-center gap-1.5">
          <button
            type="button"
            aria-label="Recommend more like this"
            aria-pressed={signal === "up"}
            disabled={submitting}
            onClick={() => sendFeedback("up")}
            className={`rounded-md border px-2.5 py-1 text-sm transition-colors ${
              signal === "up"
                ? "border-emerald-600 bg-emerald-50 text-emerald-700 dark:bg-emerald-950"
                : "border-neutral-300 dark:border-neutral-700"
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
            className={`rounded-md border px-2.5 py-1 text-sm transition-colors ${
              signal === "down"
                ? "border-red-600 bg-red-50 text-red-700 dark:bg-red-950"
                : "border-neutral-300 dark:border-neutral-700"
            }`}
          >
            👎
          </button>
        </div>
      </div>
      {error && <p className="mt-2 text-xs text-red-600">{error}</p>}
    </article>
  );
}
