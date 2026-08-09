import { HomeCta } from "../components/HomeCta";
import { TrackingDemo } from "../components/TrackingDemo";

export default function HomePage(): React.ReactElement {
  return (
    <main className="mx-auto max-w-2xl px-6 py-16">
      <h1 className="text-4xl font-bold tracking-tight sm:text-5xl">SmartReco</h1>
      <p className="mt-3 text-lg text-neutral-600 dark:text-neutral-400">
        Behavior-aware course recommendations. A LangGraph agent reads your tracked activity,
        retrieves matching courses from a vector database via RAG, and writes a grounded, persuasive
        recommendation that updates as your behavior changes.
      </p>

      <HomeCta />

      <p className="mt-8 text-sm text-neutral-500 dark:text-neutral-400">
        This page boots the non-blocking behavioral tracker (Web Worker ring buffer, batched beacons).
        The demo below exercises it directly; the catalog and dashboard pages wire the same tracker to
        real browsing, searching, and recommendation feedback.
      </p>

      <TrackingDemo />
    </main>
  );
}
