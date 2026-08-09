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
    <div className="card">
      <div className="flex flex-wrap items-center justify-between gap-3 px-4 py-3">
        <p className="text-sm text-muted">
          Based on <span className="mono text-ink">{transparency.total_events}</span> actions ·{" "}
          <span className="mono text-ink">{transparency.searches}</span> searches · updated{" "}
          {formatRelativeTime(transparency.last_generated_at)}
        </p>
        <button
          type="button"
          onClick={() => setExpanded((v) => !v)}
          aria-expanded={expanded}
          className="rounded-md border border-hairline-strong px-3 py-1.5 text-[0.82rem] text-muted transition-colors hover:border-ink hover:text-ink"
        >
          {expanded ? "Hide what we noticed" : "What we noticed about you"}
        </button>
      </div>

      {expanded && (
        <div className="border-t border-hairline px-4 py-4">
          {categories.length === 0 ? (
            <p className="text-sm text-muted">
              Not enough signal yet — browse a few courses and this fills in.
            </p>
          ) : (
            <ul className="space-y-2.5">
              {categories.map(([category, weight]) => {
                const pct = Math.round((weight / maxWeight) * 100);
                return (
                  <li key={category} className="flex items-center gap-3 text-sm">
                    <span title={category} className="w-32 shrink-0 truncate text-muted">
                      {category}
                    </span>
                    <span className="h-2 flex-1 overflow-hidden rounded-full bg-hairline">
                      <span
                        className="block h-full rounded-full bg-accent"
                        style={{ width: `${Math.max(6, pct)}%` }}
                      />
                    </span>
                    <span className="mono w-9 shrink-0 text-right text-xs text-faint">{pct}</span>
                  </li>
                );
              })}
            </ul>
          )}
        </div>
      )}
    </div>
  );
}
