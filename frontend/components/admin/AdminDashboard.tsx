"use client";
/**
 * Admin product CRUD table (PRD §6.2, A1). Lists via the public `GET /api/products` — there is no
 * admin-scoped "list including inactive" endpoint, so a soft-deleted (`is_active=false`) product drops
 * out of this table entirely rather than showing as a greyed-out row. Create/update/delete go through
 * `app/api/routes/admin.py`, which writes the Postgres row and the `vector_outbox` row in one
 * transaction (invariant #3) — `SyncStatusPanel` above the table is what proves that landed.
 */

import { useCallback, useEffect, useState } from "react";

import { apiDelete, apiGet, ApiError } from "../../lib/api";
import type { ProductOut, ProductPage } from "../../lib/types";
import { SkeletonBlock } from "../Skeleton";
import { AdminProductForm } from "./AdminProductForm";
import { SyncStatusPanel } from "./SyncStatusPanel";

const PAGE_SIZE = 50;

function formatPrice(cents: number): string {
  return cents <= 0 ? "Free" : `$${(cents / 100).toFixed(2)}`;
}

export function AdminDashboard(): React.ReactElement {
  const [page, setPage] = useState<ProductPage | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [editing, setEditing] = useState<ProductOut | null>(null);
  const [deletingId, setDeletingId] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      const result = await apiGet<ProductPage>(`/api/products?page_size=${PAGE_SIZE}`);
      setPage(result);
      setError(null);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Could not load products.");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  async function handleDelete(product: ProductOut): Promise<void> {
    if (deletingId) return;
    if (!window.confirm(`Delete "${product.title}"? This deactivates it and removes it from Qdrant.`)) {
      return;
    }
    setDeletingId(product.id);
    try {
      await apiDelete(`/api/admin/products/${product.id}`);
      if (editing?.id === product.id) setEditing(null);
      await load();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Could not delete the product.");
    } finally {
      setDeletingId(null);
    }
  }

  return (
    <div className="mt-6 space-y-6">
      <SyncStatusPanel />

      <AdminProductForm
        key={editing?.id ?? "create"}
        editing={editing}
        onSaved={() => {
          setEditing(null);
          void load();
        }}
        onCancelEdit={() => setEditing(null)}
      />

      {error && <p className="text-sm text-red-600 dark:text-red-400">{error}</p>}

      {loading ? (
        <div className="space-y-2">
          {[0, 1, 2, 3].map((i) => (
            <SkeletonBlock key={i} className="h-10 w-full" />
          ))}
        </div>
      ) : (
        <div className="overflow-x-auto rounded-lg border border-neutral-200 dark:border-neutral-800">
          <table className="w-full text-left text-sm">
            <thead className="bg-neutral-50 dark:bg-neutral-900">
              <tr>
                <th className="px-3 py-2 font-medium">Title</th>
                <th className="px-3 py-2 font-medium">Category</th>
                <th className="px-3 py-2 font-medium">Level</th>
                <th className="px-3 py-2 font-medium">Price</th>
                <th className="px-3 py-2 font-medium">Vector synced</th>
                <th className="px-3 py-2 font-medium">Actions</th>
              </tr>
            </thead>
            <tbody>
              {(page?.items ?? []).map((product) => (
                <tr
                  key={product.id}
                  className="border-t border-neutral-200 hover:bg-neutral-50 dark:border-neutral-800 dark:hover:bg-neutral-900"
                >
                  <td className="px-3 py-2">{product.title}</td>
                  <td className="px-3 py-2">{product.category}</td>
                  <td className="px-3 py-2">{product.level}</td>
                  <td className="px-3 py-2 font-mono">{formatPrice(product.price_cents)}</td>
                  <td className="px-3 py-2">
                    {product.vector_synced_at ? (
                      <span className="inline-flex items-center gap-1.5 text-emerald-700 dark:text-emerald-400">
                        <span className="h-2 w-2 rounded-full bg-emerald-500" />
                        Synced
                      </span>
                    ) : (
                      <span className="inline-flex items-center gap-1.5 text-amber-700 dark:text-amber-400">
                        <span className="h-2 w-2 rounded-full bg-amber-500" />
                        Pending
                      </span>
                    )}
                  </td>
                  <td className="px-3 py-2">
                    <div className="flex gap-2">
                      <button
                        type="button"
                        onClick={() => setEditing(product)}
                        className="rounded-md border border-neutral-300 px-3 py-1.5 text-sm dark:border-neutral-700"
                      >
                        Edit
                      </button>
                      <button
                        type="button"
                        onClick={() => void handleDelete(product)}
                        disabled={deletingId === product.id}
                        className="rounded-md border border-red-300 px-3 py-1.5 text-sm text-red-700 disabled:opacity-50 dark:border-red-900 dark:text-red-400"
                      >
                        {deletingId === product.id ? "Deleting…" : "Delete"}
                      </button>
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          {(page?.items.length ?? 0) === 0 && (
            <p className="p-4 text-center text-sm text-neutral-600 dark:text-neutral-400">
              No active products yet.
            </p>
          )}
        </div>
      )}
    </div>
  );
}
