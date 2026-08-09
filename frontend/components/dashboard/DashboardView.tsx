"use client";
/**
 * `/dashboard` (PRD §6.6, U3/U4). Renders the current recommendation — headline, narrative, cards each
 * with a "why this" line — plus the transparency strip and the "what we noticed about you" panel.
 *
 * Fetching lives here (a Client Component) rather than in the server-rendered page shell because the
 * data is per-user and cookie-authenticated (no browser cookie jar exists during SSR — see
 * `lib/api.ts`), and because the whole point of this view is to *change live*: it polls
 * `GET /api/recommendations/current` while the tab is visible and shows the U4 "Updated just now"
 * banner the moment a new `recommendation_id` shows up, exactly the behavior change the trigger
 * policy (Phase 7) is built to produce. Loading is always a skeleton, never a blocking spinner.
 */

import { useRouter } from "next/navigation";
import { useCallback, useEffect, useRef, useState } from "react";

import { apiGet, apiPost, ApiError } from "../../lib/api";
import { tracker } from "../../lib/tracker";
import type { CurrentRecommendationOut, RefreshOut } from "../../lib/types";
import { useVisiblePolling } from "../../lib/useVisiblePolling";
import { useAuth } from "../AuthProvider";
import { SkeletonBlock } from "../Skeleton";
import { AnnotatedRecommendation } from "./AnnotatedRecommendation";
import { TransparencyStrip } from "./TransparencyStrip";

const POLL_INTERVAL_MS = 8_000;
const UPDATED_BANNER_MS = 8_000;

type LoadState = "loading" | "ready" | "empty" | "error";

function DashboardSkeleton(): React.ReactElement {
  return (
    <div className="card mt-6 p-6" aria-busy="true" aria-label="Loading your recommendation">
      <SkeletonBlock className="h-4 w-40" />
      <SkeletonBlock className="mt-4 h-8 w-2/3" />
      <SkeletonBlock className="mt-3 h-4 w-full" />
      <SkeletonBlock className="mt-2 h-4 w-5/6" />
      <div className="mt-6 space-y-3">
        {[0, 1, 2].map((i) => (
          <SkeletonBlock key={i} className="h-12 w-full" />
        ))}
      </div>
    </div>
  );
}

export function DashboardView(): React.ReactElement {
  const { user, loading: authLoading } = useAuth();
  const router = useRouter();
  const [state, setState] = useState<LoadState>("loading");
  const [rec, setRec] = useState<CurrentRecommendationOut | null>(null);
  const [justUpdated, setJustUpdated] = useState(false);
  const [refreshing, setRefreshing] = useState(false);
  const [refreshMessage, setRefreshMessage] = useState<string | null>(null);
  const lastRecId = useRef<string | null>(null);
  const updatedTimer = useRef<ReturnType<typeof setTimeout> | null>(null);

  const load = useCallback(async (opts: { silent?: boolean } = {}) => {
    if (!opts.silent) setState("loading");
    try {
      const current = await apiGet<CurrentRecommendationOut>("/api/recommendations/current");
      setRec(current);
      setState("ready");
      if (lastRecId.current !== null && lastRecId.current !== current.recommendation_id) {
        setJustUpdated(true);
        if (updatedTimer.current) clearTimeout(updatedTimer.current);
        updatedTimer.current = setTimeout(() => setJustUpdated(false), UPDATED_BANNER_MS);
      }
      lastRecId.current = current.recommendation_id;
    } catch (err) {
      if (err instanceof ApiError && err.status === 404) {
        setRec(null);
        setState("empty");
      } else {
        setState("error");
      }
    }
  }, []);

  // Initial load + page_view once the auth probe resolves a logged-in user.
  useEffect(() => {
    if (!user) return;
    tracker.trackView();
    void load();
  }, [user, load]);

  // Poll for the U4 behavior-change affordance while the tab is visible (pauses when hidden, via
  // `useVisiblePolling`), so an update produced by the trigger policy (Phase 7) shows up without a
  // manual refresh.
  useVisiblePolling(() => void load({ silent: true }), POLL_INTERVAL_MS, Boolean(user));

  useEffect(() => () => {
    if (updatedTimer.current) clearTimeout(updatedTimer.current);
  }, []);

  // The dashboard requires auth — bounce logged-out visitors to /login rather than showing it.
  useEffect(() => {
    if (!authLoading && !user) router.replace("/login");
  }, [authLoading, user, router]);

  async function handleRefresh(): Promise<void> {
    if (refreshing) return;
    setRefreshing(true);
    setRefreshMessage(null);
    try {
      await apiPost<RefreshOut>("/api/recommendations/refresh");
      await load({ silent: true });
    } catch (err) {
      if (err instanceof ApiError && err.status === 429) {
        setRefreshMessage("Refreshed too recently — try again in a few seconds.");
      } else if (err instanceof ApiError && err.status === 409) {
        setRefreshMessage("Already generating your next recommendation — hang tight.");
      } else {
        setRefreshMessage(err instanceof ApiError ? err.message : "Could not refresh right now.");
      }
    } finally {
      setRefreshing(false);
    }
  }

  // While auth resolves, or while redirecting an unauthenticated visitor to /login, show the skeleton.
  if (authLoading || !user) {
    return (
      <main className="mx-auto max-w-3xl px-6 py-12">
        <DashboardSkeleton />
      </main>
    );
  }

  return (
    <main className="mx-auto max-w-3xl px-6 py-12">
      <div className="flex items-end justify-between gap-4">
        <div>
          <p className="eyebrow">Your dashboard</p>
          <h1 className="mt-2 text-3xl">Recommendations, with receipts</h1>
        </div>
        <button
          type="button"
          onClick={handleRefresh}
          disabled={refreshing || state === "loading"}
          className="btn btn-o"
        >
          {refreshing ? "Refreshing…" : "Refresh now"}
        </button>
      </div>
      {refreshMessage && <p className="mt-3 text-sm text-neg">{refreshMessage}</p>}

      {justUpdated && rec && (
        <p
          role="status"
          className="mono mt-3 inline-flex items-center gap-2 rounded-full border px-3 py-1 text-[0.78rem] text-ok"
          style={{ borderColor: "color-mix(in srgb, var(--ok) 35%, transparent)" }}
        >
          <span className="h-1.5 w-1.5 rounded-full bg-ok" aria-hidden />
          Updated just now · based on your last {rec.transparency.total_events} actions
        </p>
      )}

      {state === "loading" && <DashboardSkeleton />}

      {state === "error" && (
        <div
          className="mt-6 rounded-xl border p-4 text-sm text-neg"
          style={{
            borderColor: "color-mix(in srgb, var(--neg) 30%, transparent)",
            background: "color-mix(in srgb, var(--neg) 8%, transparent)",
          }}
        >
          Could not load your recommendation right now.{" "}
          <button type="button" onClick={() => void load()} className="underline underline-offset-2">
            Try again
          </button>
        </div>
      )}

      {state === "empty" && (
        <div className="card mt-6 p-8 text-center">
          <p className="text-muted">
            No recommendation yet — browse the catalog for a bit, or generate one now.
          </p>
          <button type="button" onClick={handleRefresh} disabled={refreshing} className="btn mt-5">
            {refreshing ? "Generating…" : "Generate my first recommendation"}
          </button>
          <div className="mt-3">
            <a href="/catalog" className="text-sm text-accent underline-offset-2 hover:underline">
              Browse the catalog
            </a>
          </div>
        </div>
      )}

      {state === "ready" && rec && (
        <div className="mt-6 space-y-6">
          <AnnotatedRecommendation rec={rec} userName={user.display_name} />
          <TransparencyStrip transparency={rec.transparency} />
        </div>
      )}
    </main>
  );
}
