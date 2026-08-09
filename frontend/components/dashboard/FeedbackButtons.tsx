"use client";
/**
 * Up/down feedback for one recommended item. Posts to `POST /api/recommendations/feedback`, which
 * writes a `feedback` event feeding the *next* trigger cycle (see `app/api/routes/recommendations.py`).
 * Optimistic: the button state flips immediately and rolls back only if the request fails. Extracted
 * from the old RecommendationCard so it can live inside the annotated recommendation rows.
 */

import { useState } from "react";

import { apiPost, ApiError } from "../../lib/api";
import { tracker } from "../../lib/tracker";
import type { FeedbackSignal } from "../../lib/types";

export function FeedbackButtons({
  recId,
  itemId,
}: {
  recId: string;
  itemId: string;
}): React.ReactElement {
  const [signal, setSignal] = useState<FeedbackSignal | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function send(next: FeedbackSignal): Promise<void> {
    if (submitting) return;
    const previous = signal;
    setSignal(next); // optimistic — never block the UI on the network round trip
    setSubmitting(true);
    setError(null);
    try {
      await apiPost("/api/recommendations/feedback", {
        rec_id: recId,
        item_id: itemId,
        signal: next,
      });
      tracker.trackClick(itemId, { source: "dashboard_feedback", signal: next });
    } catch (err) {
      setSignal(previous);
      setError(err instanceof ApiError ? err.message : "Could not record feedback");
    } finally {
      setSubmitting(false);
    }
  }

  const base =
    "flex h-8 w-8 items-center justify-center rounded-lg border font-mono text-sm transition-colors disabled:opacity-50";

  return (
    <div className="flex items-center gap-1.5" role="group" aria-label="Feedback on this recommendation">
      <button
        type="button"
        aria-label="Recommend more like this"
        aria-pressed={signal === "up"}
        disabled={submitting}
        onClick={() => send("up")}
        title="More like this"
        className={`${base} ${
          signal === "up"
            ? "border-ok text-ok"
            : "border-hairline-strong text-muted hover:border-ink hover:text-ink"
        }`}
      >
        ↑
      </button>
      <button
        type="button"
        aria-label="Show me less like this"
        aria-pressed={signal === "down"}
        disabled={submitting}
        onClick={() => send("down")}
        title="Less like this"
        className={`${base} ${
          signal === "down"
            ? "border-neg text-neg"
            : "border-hairline-strong text-muted hover:border-ink hover:text-ink"
        }`}
      >
        ↓
      </button>
      {error && <span className="sr-only">{error}</span>}
    </div>
  );
}
