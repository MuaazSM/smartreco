"use client";
/**
 * "I'm interested" (click) / "Add to cart" (cart) actions on a product detail page — the two
 * highest-signal event types in the server-side weighting table (PRD §6.3: click 1.5, cart 3.0).
 */

import { useState } from "react";

import { tracker } from "../../lib/tracker";

export function ProductActions({ productId }: { productId: string }): React.ReactElement {
  const [added, setAdded] = useState(false);

  return (
    <div className="mt-6 flex gap-2">
      <button
        type="button"
        onClick={() => tracker.trackClick(productId, { source: "product_detail" })}
        className="rounded-md border border-neutral-300 px-4 py-2 text-sm font-medium dark:border-neutral-700"
      >
        I&apos;m interested
      </button>
      <button
        type="button"
        onClick={() => {
          tracker.trackCart(productId);
          setAdded(true);
        }}
        className="rounded-md bg-neutral-900 px-4 py-2 text-sm font-medium text-white dark:bg-white dark:text-neutral-900"
      >
        {added ? "Added ✓" : "Add to cart"}
      </button>
    </div>
  );
}
