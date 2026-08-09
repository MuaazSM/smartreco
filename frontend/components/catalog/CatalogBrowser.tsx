"use client";
/**
 * The interactive half of `/catalog` (PRD §6.2, U2): search box, category/level filters, pagination.
 * Server-rendered `initialData` paints instantly; this component re-fetches only when a filter
 * changes, debounced 400ms to match the tracker's own search debounce (PRD §6.3) so a fast typist
 * doesn't fire a request — or a tracked `search` event — per keystroke.
 *
 * `GET /api/products?q=` is hybrid semantic-search-with-keyword-fallback on the backend
 * (`app/vector/hybrid_retriever.py` territory) — this component just forwards the query string.
 */

import { useEffect, useRef, useState } from "react";

import { apiGet, ApiError } from "../../lib/api";
import { tracker } from "../../lib/tracker";
import type { ProductPage } from "../../lib/types";
import { SkeletonBlock } from "../Skeleton";
import { ProductCard } from "./ProductCard";

export interface CatalogQuery {
  q: string;
  category: string;
  level: string;
  page: number;
  page_size: number;
}

// Mirrors scripts/seed_data.py's fixed 8-category seed taxonomy. There is no `GET /api/categories`
// endpoint, so this is a lightweight, hand-maintained filter rather than a live lookup — a category an
// admin adds later won't appear here until this list is updated too.
const CATEGORIES = [
  "Data Science",
  "Machine Learning",
  "AI & LLMs",
  "Web Development",
  "Cloud & DevOps",
  "Programming",
  "Cybersecurity",
  "Design",
];
const LEVELS = ["beginner", "intermediate", "advanced"];

const FETCH_DEBOUNCE_MS = 400;

function buildQueryString(q: CatalogQuery): string {
  const params = new URLSearchParams();
  if (q.q) params.set("q", q.q);
  if (q.category) params.set("category", q.category);
  if (q.level) params.set("level", q.level);
  params.set("page", String(q.page));
  params.set("page_size", String(q.page_size));
  return params.toString();
}

export function CatalogBrowser({
  initialQuery,
  initialData,
}: {
  initialQuery: CatalogQuery;
  initialData: ProductPage;
}): React.ReactElement {
  const [query, setQuery] = useState<CatalogQuery>(initialQuery);
  const [data, setData] = useState<ProductPage>(initialData);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const isFirstRun = useRef(true);
  const debounceTimer = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(() => {
    // Skip the run that fires on mount — `initialData` already matches `initialQuery`.
    if (isFirstRun.current) {
      isFirstRun.current = false;
      return;
    }
    if (debounceTimer.current) clearTimeout(debounceTimer.current);
    debounceTimer.current = setTimeout(() => void fetchProducts(query), FETCH_DEBOUNCE_MS);
    return () => {
      if (debounceTimer.current) clearTimeout(debounceTimer.current);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [query.q, query.category, query.level, query.page]);

  async function fetchProducts(q: CatalogQuery): Promise<void> {
    setLoading(true);
    setError(null);
    try {
      const page = await apiGet<ProductPage>(`/api/products?${buildQueryString(q)}`);
      setData(page);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Could not load courses right now.");
    } finally {
      setLoading(false);
    }
  }

  function updateQuery(partial: Partial<CatalogQuery>): void {
    setQuery((prev) => ({ ...prev, ...partial, page: partial.page ?? 1 }));
  }

  function onSearchChange(value: string): void {
    updateQuery({ q: value });
    tracker.trackSearch(value); // the tracker debounces its own 400ms before emitting the event
  }

  const totalPages = Math.max(1, Math.ceil(data.total / data.page_size));

  return (
    <div className="mt-6">
      <div className="flex flex-wrap gap-3">
        <input
          type="search"
          value={query.q}
          onChange={(e) => onSearchChange(e.target.value)}
          placeholder="Search courses (e.g. deep learning, react hooks)"
          className="field min-w-[16rem] flex-1"
        />
        <select
          value={query.category}
          onChange={(e) => updateQuery({ category: e.target.value })}
          className="field"
        >
          <option value="">All categories</option>
          {CATEGORIES.map((c) => (
            <option key={c} value={c}>
              {c}
            </option>
          ))}
        </select>
        <select
          value={query.level}
          onChange={(e) => updateQuery({ level: e.target.value })}
          className="field"
        >
          <option value="">All levels</option>
          {LEVELS.map((l) => (
            <option key={l} value={l}>
              {l}
            </option>
          ))}
        </select>
      </div>

      {error && <p className="mt-3 text-sm text-neg">{error}</p>}

      <div className="mt-6 grid gap-4 sm:grid-cols-2 lg:grid-cols-3" aria-busy={loading}>
        {loading
          ? Array.from({ length: 6 }).map((_, i) => <SkeletonBlock key={i} className="h-40 w-full" />)
          : data.items.map((product) => <ProductCard key={product.id} product={product} />)}
      </div>

      {!loading && data.items.length === 0 && (
        <p className="mt-8 text-center text-muted">No courses match your filters.</p>
      )}

      {data.total > data.page_size && (
        <div className="mt-8 flex items-center justify-center gap-4 text-sm">
          <button
            type="button"
            disabled={query.page <= 1}
            onClick={() => updateQuery({ page: query.page - 1 })}
            className="rounded-md border border-hairline-strong px-3 py-1.5 text-muted transition-colors hover:border-ink hover:text-ink disabled:opacity-40 disabled:hover:border-hairline-strong disabled:hover:text-muted"
          >
            Previous
          </button>
          <span className="text-muted">
            Page <span className="mono text-ink">{query.page}</span> of{" "}
            <span className="mono text-ink">{totalPages}</span>
          </span>
          <button
            type="button"
            disabled={query.page >= totalPages}
            onClick={() => updateQuery({ page: query.page + 1 })}
            className="rounded-md border border-hairline-strong px-3 py-1.5 text-muted transition-colors hover:border-ink hover:text-ink disabled:opacity-40 disabled:hover:border-hairline-strong disabled:hover:text-muted"
          >
            Next
          </button>
        </div>
      )}
    </div>
  );
}
