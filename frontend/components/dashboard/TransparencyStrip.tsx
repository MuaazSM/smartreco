"use client";
/**
 * "Based on 14 actions · 3 searches · updated 2m ago" strip, plus the expandable "what we noticed
 * about you" panel of top categories (PRD §6.6). Recomputes the relative-time text on an interval so
 * "updated 2m ago" keeps advancing without a page refresh.
 */

import { useEffect, useState } from "react";

import { formatRelativeTime } from "../../lib/time";
import type { Transparency } from "../../lib/types";

export function TransparencyStrip({ transparency }: { transparency: Transparency }): React.ReactElement {
  const [expanded, setExpanded] = useState(false);
  const [, forceTick] = useState(0);

  // Re-render every 30s so the "updated Xm ago" text stays fresh without a manual refresh.
  useEffect(() => {
    const id = setInterval(() => forceTick((n) => n + 1), 30_000);
    return () => clearInterval(id);
  }, []);

  const categories = Object.entries(transparency.top_categories).sort((a, b) => b[1] - a[1]);
  const maxWeight = categories.length > 0 ? categories[0][1] : 1;

  return (
    <div className="mt-4 rounded-lg border border-neutral-200 bg-neutral-50 dark:border-neutral-800 dark:bg-neutral-900">
      <div className="flex flex-wrap items-center justify-between gap-2 px-4 py-3">
        <p className="text-sm text-neutral-600 dark:text-neutral-400">
          Based on{" "}
          <span className="font-mono font-medium text-neutral-900 dark:text-neutral-100">
            {transparency.total_events}
          </span>{" "}
          actions ·{" "}
          <span className="font-mono font-medium text-neutral-900 dark:text-neutral-100">
            {transparency.searches}
          </span>{" "}
          searches · updated {formatRelativeTime(transparency.last_generated_at)}
        </p>
        <button
          type="button"
          onClick={() => setExpanded((v) => !v)}
          aria-expanded={expanded}
          className="rounded-md border border-neutral-300 px-3 py-1.5 text-sm font-medium transition-colors hover:bg-neutral-100 dark:border-neutral-700 dark:hover:bg-neutral-800"
        >
          {expanded ? "Hide what we noticed" : "What we noticed about you"}
        </button>
      </div>

      {expanded && (
        <div className="border-t border-neutral-200 px-4 py-3 dark:border-neutral-800">
          {categories.length === 0 ? (
            <p className="text-sm text-neutral-500">
              Not enough signal yet — browse a few courses and this fills in.
            </p>
          ) : (
            <ul className="space-y-1.5">
              {categories.map(([category, weight]) => (
                <li key={category} className="flex items-center gap-2 text-sm">
                  <span
                    title={category}
                    className="w-32 shrink-0 truncate text-neutral-700 dark:text-neutral-300"
                  >
                    {category}
                  </span>
                  <span className="h-2 flex-1 overflow-hidden rounded-full bg-neutral-200 dark:bg-neutral-800">
                    <span
                      className="block h-full rounded-full bg-accent"
                      style={{ width: `${Math.max(6, Math.round((weight / maxWeight) * 100))}%` }}
                    />
                  </span>
                </li>
              ))}
            </ul>
          )}
        </div>
      )}
    </div>
  );
}
