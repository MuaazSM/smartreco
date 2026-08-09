/**
 * A pulsing placeholder block. Used anywhere a page has data in flight — dashboard cards, catalog
 * grid, admin table — so loading states never fall back to a blocking spinner (Phase 8 constraint,
 * PRD §6.6).
 */
export function SkeletonBlock({ className = "" }: { className?: string }): React.ReactElement {
  return <div className={`animate-pulse rounded-md bg-hairline ${className}`} />;
}
