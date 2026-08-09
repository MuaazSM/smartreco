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
      <p className="mt-3 text-sm text-neutral-500">
        This page boots the non-blocking behavioral tracker (Web Worker ring buffer, batched beacons).
        The dashboard and catalog UI land in Phase 8.
      </p>

      <TrackingDemo />
    </main>
  );
}
