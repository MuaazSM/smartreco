"use client";
/**
 * Wraps server-rendered product detail markup to fire the tracker's `view` event on mount and observe
 * dwell time (PRD §6.3). Server Components can be passed as `children` into a Client Component without
 * becoming Client Components themselves, so the actual product markup (title/description/tags) stays
 * server-rendered — only this thin wrapper needs `"use client"`.
 */

import { useEffect, useRef } from "react";

import { tracker } from "../../lib/tracker";

export function ProductDetailTracker({
  productId,
  children,
}: {
  productId: string;
  children: React.ReactNode;
}): React.ReactElement {
  const ref = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    tracker.trackView(productId);
  }, [productId]);

  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    // Emits exactly one `dwell` event, carrying accumulated foreground-visible time, on unmount.
    return tracker.observeDwell(el, productId);
  }, [productId]);

  return <div ref={ref}>{children}</div>;
}
