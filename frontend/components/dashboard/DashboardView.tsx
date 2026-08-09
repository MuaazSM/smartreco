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

import { useCallback, useEffect, useRef, useState } from "react";

import { apiGet, apiPost, ApiError } from "../../lib/api";
import { tracker } from "../../lib/tracker";
import type { CurrentRecommendationOut, RefreshOut } from "../../lib/types";
import { useVisiblePolling } from "../../lib/useVisiblePolling";
import { useAuth } from "../AuthProvider";
import { SkeletonBlock } from "../Skeleton";
import { RecommendationCard } from "./RecommendationCard";
import { TransparencyStrip } from "./TransparencyStrip";

const POLL_INTERVAL_MS = 8_000;
const UPDATED_BANNER_MS = 8_000;

type LoadState = "loading" | "ready" | "empty" | "error";

function DashboardSkeleton(): React.ReactElement {
  return (
    <div aria-busy="true" aria-label="Loading your recommendation">
      <SkeletonBlock className="h-8 w-2/3" />
      <SkeletonBlock className="mt-3 h-4 w-full" />
      <SkeletonBlock className="mt-2 h-4 w-5/6" />
      <SkeletonBlock className="mt-6 h-12 w-full" />
      <div className="mt-6 grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
        {[0, 1, 2].map((i) => (
          <SkeletonBlock key={i} className="h-48 w-full" />
        ))}
      </div>
    </div>
  );
}

export function DashboardView(): React.ReactElement {
  const { user, loading: authLoading } = useAuth();
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

  if (authLoading) {
    return (
      <main className="mx-auto max-w-4xl px-6 py-12">
        <DashboardSkeleton />
      </main>
    );
  }

  if (!user) {
    return (
      <main className="mx-auto max-w-4xl px-6 py-12">
        <h1 className="text-2xl font-bold">Your recommendations</h1>
        <p className="mt-3 text-neutral-600 dark:text-neutral-400">
          Log in to see a personalized recommendation block built from your activity.
        </p>
        <a
          href="/login"
          className="mt-4 inline-block rounded-md bg-neutral-900 px-4 py-2 text-sm font-medium text-white dark:bg-white dark:text-neutral-900"
        >
          Log in
        </a>
      </main>
    );
  }

  return (
    <main className="mx-auto max-w-4xl px-6 py-12">
      <div className="flex items-start justify-between gap-4">
        <h1 className="text-2xl font-bold">Your recommendations</h1>
        <button
          type="button"
          onClick={handleRefresh}
          disabled={refreshing || state === "loading"}
          className="whitespace-nowrap rounded-md border border-neutral-300 px-3 py-1.5 text-sm font-medium disabled:opacity-50 dark:border-neutral-700"
        >
          {refreshing ? "Refreshing…" : "Refresh now"}
        </button>
      </div>
      {refreshMessage && <p className="mt-2 text-sm text-amber-600">{refreshMessage}</p>}

      {justUpdated && rec && (
        <p
          role="status"
          className="mt-3 inline-flex items-center gap-1.5 rounded-full bg-emerald-50 px-3 py-1 text-sm font-medium text-emerald-700 dark:bg-emerald-950 dark:text-emerald-300"
        >
          Updated just now · based on your last {rec.transparency.total_events} actions
        </p>
      )}

      {state === "loading" && <DashboardSkeleton />}

      {state === "error" && (
        <div className="mt-6 rounded-lg border border-red-200 bg-red-50 p-4 text-sm text-red-700 dark:border-red-900 dark:bg-red-950 dark:text-red-300">
          Could not load your recommendation right now.{" "}
          <button type="button" onClick={() => void load()} className="underline underline-offset-2">
            Try again
          </button>
        </div>
      )}

      {state === "empty" && (
        <div className="mt-6 rounded-lg border border-neutral-200 p-6 text-center dark:border-neutral-800">
          <p className="text-neutral-600 dark:text-neutral-400">
            No recommendation yet — browse the catalog for a bit, or generate one now.
          </p>
          <button
            type="button"
            onClick={handleRefresh}
            disabled={refreshing}
            className="mt-4 rounded-md bg-neutral-900 px-4 py-2 text-sm font-medium text-white disabled:opacity-50 dark:bg-white dark:text-neutral-900"
          >
            {refreshing ? "Generating…" : "Generate my first recommendation"}
          </button>
          <a href="/catalog" className="mt-3 block text-sm underline underline-offset-2">
            Browse the catalog
          </a>
        </div>
      )}

      {state === "ready" && rec && (
        <>
          <h2 className="mt-6 text-xl font-semibold">{rec.headline}</h2>
          <p className="mt-2 text-neutral-600 dark:text-neutral-400">{rec.narrative}</p>

          <TransparencyStrip transparency={rec.transparency} />

          <div className="mt-6 grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
            {rec.items.map((item) => (
              <RecommendationCard key={item.product_id} item={item} recId={rec.recommendation_id} />
            ))}
          </div>
        </>
      )}
    </main>
  );
}
