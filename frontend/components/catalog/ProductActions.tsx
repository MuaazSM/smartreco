"use client";
/**
 * "I'm interested" (click) / "Add to cart" (cart) actions on a product detail page — the two
 * highest-signal event types in the server-side weighting table (PRD §6.3: click 1.5, cart 3.0).
 */

import { useState } from "react";

import { tracker } from "../../lib/tracker";

export function ProductActions({ productId }: { productId: string }): React.ReactElement {
  const [interested, setInterested] = useState(false);
  const [added, setAdded] = useState(false);

  return (
    <div className="mt-6 flex gap-2.5">
      <button
        type="button"
        aria-pressed={interested}
        onClick={() => {
          tracker.trackClick(productId, { source: "product_detail" });
          setInterested(true);
        }}
        className={`btn btn-o ${interested ? "!border-ok !text-ok" : ""}`}
      >
        {interested ? "Interested ✓" : "I'm interested"}
      </button>
      <button
        type="button"
        onClick={() => {
          tracker.trackCart(productId);
          setAdded(true);
        }}
        className="btn"
      >
        {added ? "Added ✓" : "Add to cart"}
      </button>
    </div>
  );
}
