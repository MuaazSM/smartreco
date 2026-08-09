import type { Metadata } from "next";
import { notFound } from "next/navigation";

import { ProductActions } from "../../../components/catalog/ProductActions";
import { ProductDetailTracker } from "../../../components/catalog/ProductDetailTracker";
import { apiGet, ApiError } from "../../../lib/api";
import type { ProductOut } from "../../../lib/types";

export const metadata: Metadata = {
  title: "Course · SmartReco",
};

function formatPrice(cents: number): string {
  return cents <= 0 ? "Free" : `$${(cents / 100).toFixed(2)}`;
}

// Server Component: fetches the public, unauthenticated `GET /api/products/{id}` directly (no cookie
// needed). A genuine 404 from the backend maps to Next's `notFound()`; any other failure (e.g. the
// backend being unreachable during a local build/preview) renders a soft fallback instead of crashing.
export default async function ProductDetailPage({
  params,
}: {
  params: Promise<{ id: string }>;
}): Promise<React.ReactElement> {
  const { id } = await params;

  let product: ProductOut;
  try {
    product = await apiGet<ProductOut>(`/api/products/${id}`);
  } catch (err) {
    if (err instanceof ApiError && err.status === 404) notFound();
    return (
      <main className="mx-auto max-w-3xl px-7 py-12">
        <p className="text-muted">
          Could not load this course right now.{" "}
          <a href="/catalog" className="text-accent underline-offset-2 hover:underline">
            Back to catalog
          </a>
        </p>
      </main>
    );
  }

  return (
    <main className="mx-auto max-w-3xl px-7 py-12">
      <ProductDetailTracker productId={product.id}>
        <a
          href="/catalog"
          className="mono text-sm text-muted transition-colors hover:text-ink"
        >
          ← back to catalog
        </a>
        <div className="mt-5 flex items-start justify-between gap-5">
          <div>
            <p className="eyebrow">
              {product.category} · {product.level}
            </p>
            <h1 className="mt-2 text-[clamp(2rem,4vw,2.75rem)] leading-[1.05]">{product.title}</h1>
          </div>
          <span className="mono shrink-0 whitespace-nowrap text-xl font-semibold">
            {formatPrice(product.price_cents)}
          </span>
        </div>

        <p className="mt-5 text-muted">{product.description}</p>

        {product.tags.length > 0 && (
          <div className="mt-5 flex flex-wrap gap-1.5">
            {product.tags.map((tag) => (
              <span key={tag} className="tag">
                {tag}
              </span>
            ))}
          </div>
        )}

        <ProductActions productId={product.id} />
      </ProductDetailTracker>
    </main>
  );
}
