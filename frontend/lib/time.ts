/**
 * Tiny relative-time formatter for the transparency strip ("updated 2m ago") and the U4 "Updated just
 * now" affordance. No date library — this is the one thing it needs to do (CLAUDE.md: don't add a
 * dependency without a one-sentence reason).
 */
export function formatRelativeTime(iso: string | null | undefined, nowMs: number = Date.now()): string {
  if (!iso) return "never";
  const then = new Date(iso).getTime();
  if (Number.isNaN(then)) return "never";

  const diffSeconds = Math.max(0, Math.round((nowMs - then) / 1000));
  if (diffSeconds < 45) return "just now";
  if (diffSeconds < 90) return "1m ago";

  const minutes = Math.round(diffSeconds / 60);
  if (minutes < 60) return `${minutes}m ago`;

  const hours = Math.round(minutes / 60);
  if (hours < 24) return `${hours}h ago`;

  const days = Math.round(hours / 24);
  return `${days}d ago`;
}
