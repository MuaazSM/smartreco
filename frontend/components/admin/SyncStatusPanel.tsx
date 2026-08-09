"use client";
/**
 * `GET /api/admin/sync-status` readout (PRD §6.2, A1/A3). "in_sync: true" is, per the backend's own
 * docstring, "the single most demonstrable claim in the submission" — this panel is a thin, honest
 * mirror of that response: green when in sync, amber/red with the concrete drift counts otherwise.
 * Polls while the tab is visible so a product edit's outbox drain (every 5s server-side) is visible
 * settling in near-real-time.
 */

import { useCallback, useEffect, useState } from "react";

import { apiGet, ApiError } from "../../lib/api";
import { SkeletonBlock } from "../Skeleton";
import type { SyncStatus } from "../../lib/types";
import { useVisiblePolling } from "../../lib/useVisiblePolling";

const POLL_INTERVAL_MS = 10_000;

function esc(s: string): string {
  return s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}

// Render the live sync-status as the artifact's "receipts" console — the same shape the landing
// page shows statically. Dynamic ids are HTML-escaped before interpolation.
function consoleHtml(status: SyncStatus): string {
  const k = (v: string): string => `<span class="k">${v}</span>`;
  const num = (v: string): string => `<span class="s">${v}</span>`;
  const arr = (a: string[]): string =>
    a.length === 0
      ? "[]"
      : `[ ${a
          .slice(0, 3)
          .map((x) => `<span class="s">"${esc(x)}"</span>`)
          .join(", ")}${a.length > 3 ? `, <span class="dim">…+${a.length - 3}</span>` : ""} ]`;
  const inSync = status.in_sync
    ? `<span class="g">true</span>`
    : `<span class="s" style="color:var(--neg)">false</span>`;
  return [
    `<span class="c">$ GET /api/admin/sync-status</span>`,
    `{`,
    `  ${k('"in_sync"')}: ${inSync},`,
    `  ${k('"missing_in_vector"')}: ${arr(status.missing_in_vector)},`,
    `  ${k('"orphaned_in_vector"')}: ${arr(status.orphaned_in_vector)},`,
    `  ${k('"pending_count"')}: ${num(String(status.pending_count))},`,
    `  ${k('"failed_count"')}: ${num(String(status.failed_count))},`,
    `  ${k('"outbox_lag_seconds"')}: ${num(status.outbox_lag_seconds.toFixed(1))}`,
    `}`,
  ].join("\n");
}

export function SyncStatusPanel(): React.ReactElement {
  const [status, setStatus] = useState<SyncStatus | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      const current = await apiGet<SyncStatus>("/api/admin/sync-status");
      setStatus(current);
      setError(null);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Could not load sync status.");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  useVisiblePolling(() => void load(), POLL_INTERVAL_MS, true);

  if (loading) {
    return (
      <div className="card p-4">
        <SkeletonBlock className="h-5 w-40" />
        <SkeletonBlock className="mt-3 h-4 w-full" />
      </div>
    );
  }

  if (error || !status) {
    return (
      <div
        className="rounded-xl border p-4 text-sm text-neg"
        style={{
          borderColor: "color-mix(in srgb, var(--neg) 30%, transparent)",
          background: "color-mix(in srgb, var(--neg) 8%, transparent)",
        }}
      >
        {error ?? "Sync status unavailable."}
      </div>
    );
  }

  const metrics: [string, number][] = [
    ["Missing in vector", status.missing_in_vector.length],
    ["Orphaned in vector", status.orphaned_in_vector.length],
    ["Pending outbox", status.pending_count],
    ["Failed outbox", status.failed_count],
  ];

  return (
    <div className="space-y-3">
      <div className="card p-4">
        <div className="flex items-center gap-2.5">
          <span
            className="h-2.5 w-2.5 rounded-full"
            style={{ background: status.in_sync ? "var(--ok)" : "var(--neg)" }}
          />
          <p className="font-medium" style={{ color: status.in_sync ? "var(--ok)" : "var(--neg)" }}>
            {status.in_sync ? "Postgres and Qdrant are in sync" : "Vector store drift detected"}
          </p>
        </div>
        <dl className="mt-4 grid grid-cols-2 gap-x-6 gap-y-2 text-sm sm:grid-cols-4">
          {metrics.map(([label, value]) => (
            <div key={label}>
              <dt className="text-muted">{label}</dt>
              <dd className="mono mt-0.5 font-medium">{value}</dd>
            </div>
          ))}
        </dl>
        <p className="mt-3 text-xs text-faint">
          Outbox lag: <span className="mono">{status.outbox_lag_seconds.toFixed(1)}s</span>
        </p>
      </div>

      <div className="console">
        <div className="console-bar">
          <span className="console-dot" />
          <span className="console-dot" />
          <span className="console-dot" />
          <span className="console-ttl">smartreco — admin · sync-status</span>
        </div>
        <pre dangerouslySetInnerHTML={{ __html: consoleHtml(status) }} />
      </div>
    </div>
  );
}
