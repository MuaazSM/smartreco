import type { Metadata } from "next";

import { CatalogBrowser, type CatalogQuery } from "../../components/catalog/CatalogBrowser";
import { apiGet } from "../../lib/api";
import type { ProductPage } from "../../lib/types";

export const metadata: Metadata = {
  title: "Catalog · SmartReco",
};

const DEFAULT_PAGE_SIZE = 21;

function buildQueryString(q: CatalogQuery): string {
  const params = new URLSearchParams();
  if (q.q) params.set("q", q.q);
  if (q.category) params.set("category", q.category);
  if (q.level) params.set("level", q.level);
  params.set("page", String(q.page));
  params.set("page_size", String(q.page_size));
  return params.toString();
}

function firstString(value: string | string[] | undefined): string {
  return typeof value === "string" ? value : "";
}

// Server Component shell: SSR's the first page of results from the *public, unauthenticated*
// `GET /api/products` (no cookie needed — see lib/api.ts), then hands off to `CatalogBrowser`
// ("use client") for search/filter interactivity. `cache: "no-store"` inside `apiGet` already makes
// this route dynamic, so `npm run build` does not need a live backend to succeed.
export default async function CatalogPage({
  searchParams,
}: {
  searchParams: Promise<Record<string, string | string[] | undefined>>;
}): Promise<React.ReactElement> {
  const sp = await searchParams;
  const initialQuery: CatalogQuery = {
    q: firstString(sp.q),
    category: firstString(sp.category),
    level: firstString(sp.level),
    page: Number(firstString(sp.page)) || 1,
    page_size: DEFAULT_PAGE_SIZE,
  };

  let initialData: ProductPage = {
    items: [],
    total: 0,
    page: initialQuery.page,
    page_size: initialQuery.page_size,
  };
  try {
    initialData = await apiGet<ProductPage>(`/api/products?${buildQueryString(initialQuery)}`);
  } catch {
    // Backend unreachable (e.g. a local build/preview without the API running) — CatalogBrowser
    // re-fetches client-side on the first filter interaction regardless.
  }

  return (
    <main className="mx-auto max-w-5xl px-6 py-12">
      <h1 className="text-2xl font-bold">Course catalog</h1>
      <p className="mt-2 text-neutral-600 dark:text-neutral-400">
        Search is semantic with keyword fallback — every search you run is tracked and feeds your
        recommendation.
      </p>
      <CatalogBrowser initialQuery={initialQuery} initialData={initialData} />
    </main>
  );
}
