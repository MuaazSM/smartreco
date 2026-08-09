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
      <div className="rounded-lg border border-neutral-200 p-4 dark:border-neutral-800">
        <SkeletonBlock className="h-5 w-40" />
        <SkeletonBlock className="mt-3 h-4 w-full" />
      </div>
    );
  }

  if (error || !status) {
    return (
      <div className="rounded-lg border border-red-200 bg-red-50 p-4 text-sm text-red-700 dark:border-red-900 dark:bg-red-950 dark:text-red-300">
        {error ?? "Sync status unavailable."}
      </div>
    );
  }

  return (
    <div
      className={`rounded-lg border p-4 ${
        status.in_sync
          ? "border-emerald-200 bg-emerald-50 dark:border-emerald-900 dark:bg-emerald-950"
          : "border-amber-200 bg-amber-50 dark:border-amber-900 dark:bg-amber-950"
      }`}
    >
      <div className="flex items-center gap-2">
        <span
          className={`h-2.5 w-2.5 rounded-full ${status.in_sync ? "bg-emerald-500" : "bg-amber-500"}`}
        />
        <p className="font-semibold">
          {status.in_sync ? "Postgres and Qdrant are in sync" : "Vector store drift detected"}
        </p>
      </div>
      <dl className="mt-3 grid grid-cols-2 gap-x-4 gap-y-1 text-sm sm:grid-cols-4">
        <div>
          <dt className="text-neutral-600 dark:text-neutral-400">Missing in vector</dt>
          <dd className="font-mono font-medium">{status.missing_in_vector.length}</dd>
        </div>
        <div>
          <dt className="text-neutral-600 dark:text-neutral-400">Orphaned in vector</dt>
          <dd className="font-mono font-medium">{status.orphaned_in_vector.length}</dd>
        </div>
        <div>
          <dt className="text-neutral-600 dark:text-neutral-400">Pending outbox</dt>
          <dd className="font-mono font-medium">{status.pending_count}</dd>
        </div>
        <div>
          <dt className="text-neutral-600 dark:text-neutral-400">Failed outbox</dt>
          <dd className="font-mono font-medium">{status.failed_count}</dd>
        </div>
      </dl>
      <p className="mt-2 text-xs text-neutral-500">
        Outbox lag: <span className="font-mono">{status.outbox_lag_seconds.toFixed(1)}s</span>
      </p>
    </div>
  );
}
