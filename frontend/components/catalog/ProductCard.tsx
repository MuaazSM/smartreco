/**
 * One catalog grid tile. No `"use client"` directive of its own — it is only ever rendered from
 * inside `CatalogBrowser` (a Client Component), which already establishes the client boundary; a
 * component doesn't need its own directive to use event handlers once it lives in that module graph.
 */
import Link from "next/link";

import { tracker } from "../../lib/tracker";
import type { ProductOut } from "../../lib/types";

function formatPrice(cents: number): string {
  return cents <= 0 ? "Free" : `$${(cents / 100).toFixed(2)}`;
}

export function ProductCard({ product }: { product: ProductOut }): React.ReactElement {
  return (
    <Link
      href={`/catalog/${product.id}`}
      onClick={() => tracker.trackClick(product.id, { source: "catalog_grid" })}
      className="card flex flex-col p-5 transition-colors hover:border-hairline-strong hover:shadow-artifact"
    >
      <p className="font-mono text-xs uppercase tracking-wide text-faint">
        {product.category} · {product.level}
      </p>
      <h3 className="mt-1.5 font-sans text-lg font-semibold leading-snug tracking-normal [font-variation-settings:normal]">
        {product.title}
      </h3>
      <p className="mt-2 line-clamp-2 flex-1 text-sm text-muted">{product.description}</p>
      <p className="mono mt-4 text-sm font-medium">{formatPrice(product.price_cents)}</p>
    </Link>
  );
}
