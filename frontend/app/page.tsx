import Link from "next/link";

import { TrackingDemo } from "../components/TrackingDemo";

export default function HomePage(): React.ReactElement {
  return (
    <main className="mx-auto max-w-2xl px-6 py-16">
      <h1 className="text-3xl font-bold tracking-tight">SmartReco</h1>
      <p className="mt-3 text-neutral-600 dark:text-neutral-400">
        Behavior-aware course recommendations. A LangGraph agent reads your tracked activity,
        retrieves matching courses from a vector database via RAG, and writes a grounded, persuasive
        recommendation that updates as your behavior changes.
      </p>

      <div className="mt-6 flex gap-3">
        <Link
          href="/catalog"
          className="rounded-md bg-neutral-900 px-4 py-2 text-sm font-medium text-white dark:bg-white dark:text-neutral-900"
        >
          Browse the catalog
        </Link>
        <Link
          href="/dashboard"
          className="rounded-md border border-neutral-300 px-4 py-2 text-sm font-medium dark:border-neutral-700"
        >
          See my recommendations
        </Link>
      </div>

      <p className="mt-8 text-sm text-neutral-500">
        This page boots the non-blocking behavioral tracker (Web Worker ring buffer, batched beacons).
        The demo below exercises it directly; the catalog and dashboard pages wire the same tracker to
        real browsing, searching, and recommendation feedback.
      </p>

      <TrackingDemo />
    </main>
  );
}
