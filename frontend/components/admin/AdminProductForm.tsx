"use client";
/**
 * Create/edit form for `POST /api/admin/products` and `PATCH /api/admin/products/{id}` (PRD §6.2, A1).
 * The parent (`AdminDashboard`) remounts this with a fresh `key` when the editing target changes, so
 * this component's local state only ever needs to seed from `editing` once rather than sync to it.
 */

import { useState } from "react";

import { apiPatch, apiPost, ApiError } from "../../lib/api";
import type { ProductCreateIn, ProductOut, ProductUpdateIn } from "../../lib/types";

const LEVELS = ["beginner", "intermediate", "advanced"] as const;

function centsToDollarsInput(cents: number): string {
  return (cents / 100).toFixed(2);
}

export function AdminProductForm({
  editing,
  onSaved,
  onCancelEdit,
}: {
  editing: ProductOut | null;
  onSaved: () => void;
  onCancelEdit: () => void;
}): React.ReactElement {
  const [title, setTitle] = useState(editing?.title ?? "");
  const [description, setDescription] = useState(editing?.description ?? "");
  const [category, setCategory] = useState(editing?.category ?? "");
  const [level, setLevel] = useState<(typeof LEVELS)[number]>(
    (editing?.level as (typeof LEVELS)[number]) ?? "beginner",
  );
  const [priceDollars, setPriceDollars] = useState(
    editing ? centsToDollarsInput(editing.price_cents) : "0.00",
  );
  const [tags, setTags] = useState(editing?.tags.join(", ") ?? "");
  const [isActive, setIsActive] = useState(editing?.is_active ?? true);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function onSubmit(e: React.FormEvent): Promise<void> {
    e.preventDefault();
    if (submitting) return;
    setSubmitting(true);
    setError(null);

    const priceCents = Math.round(Number.parseFloat(priceDollars || "0") * 100);
    const parsedTags = tags
      .split(",")
      .map((t) => t.trim())
      .filter(Boolean);

    try {
      if (editing) {
        const body: ProductUpdateIn = {
          title,
          description,
          category,
          level,
          price_cents: priceCents,
          tags: parsedTags,
          is_active: isActive,
        };
        await apiPatch(`/api/admin/products/${editing.id}`, body);
      } else {
        const body: ProductCreateIn = {
          title,
          description,
          category,
          level,
          price_cents: priceCents,
          tags: parsedTags,
          is_active: isActive,
        };
        await apiPost("/api/admin/products", body);
        setTitle("");
        setDescription("");
        setCategory("");
        setPriceDollars("0.00");
        setTags("");
      }
      onSaved();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Could not save the product.");
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <form
      onSubmit={onSubmit}
      className="grid gap-3 rounded-lg border border-neutral-200 p-4 dark:border-neutral-800 sm:grid-cols-2"
    >
      <h3 className="col-span-full font-semibold">{editing ? `Edit "${editing.title}"` : "New product"}</h3>

      <label className="text-sm">
        Title
        <input
          required
          value={title}
          onChange={(e) => setTitle(e.target.value)}
          className="mt-1 w-full rounded-md border border-neutral-300 bg-transparent px-3 py-2.5 text-sm dark:border-neutral-700"
        />
      </label>

      <label className="text-sm">
        Category
        <input
          required
          value={category}
          onChange={(e) => setCategory(e.target.value)}
          className="mt-1 w-full rounded-md border border-neutral-300 bg-transparent px-3 py-2.5 text-sm dark:border-neutral-700"
        />
      </label>

      <label className="col-span-full text-sm">
        Description
        <textarea
          value={description}
          onChange={(e) => setDescription(e.target.value)}
          rows={2}
          className="mt-1 w-full rounded-md border border-neutral-300 bg-transparent px-3 py-2.5 text-sm dark:border-neutral-700"
        />
      </label>

      <label className="text-sm">
        Level
        <select
          value={level}
          onChange={(e) => setLevel(e.target.value as (typeof LEVELS)[number])}
          className="mt-1 w-full rounded-md border border-neutral-300 bg-transparent px-3 py-2.5 text-sm dark:border-neutral-700"
        >
          {LEVELS.map((l) => (
            <option key={l} value={l}>
              {l}
            </option>
          ))}
        </select>
      </label>

      <label className="text-sm">
        Price (USD)
        <input
          type="number"
          min="0"
          step="0.01"
          value={priceDollars}
          onChange={(e) => setPriceDollars(e.target.value)}
          className="mt-1 w-full rounded-md border border-neutral-300 bg-transparent px-3 py-2.5 text-sm dark:border-neutral-700"
        />
      </label>

      <label className="col-span-full text-sm">
        Tags (comma-separated)
        <input
          value={tags}
          onChange={(e) => setTags(e.target.value)}
          className="mt-1 w-full rounded-md border border-neutral-300 bg-transparent px-3 py-2.5 text-sm dark:border-neutral-700"
        />
      </label>

      <label className="flex items-center gap-2 text-sm">
        <input type="checkbox" checked={isActive} onChange={(e) => setIsActive(e.target.checked)} />
        Active
      </label>

      {error && <p className="col-span-full text-sm text-red-600 dark:text-red-400">{error}</p>}

      <div className="col-span-full flex gap-2">
        <button
          type="submit"
          disabled={submitting}
          className="rounded-md bg-neutral-900 px-4 py-1.5 text-sm font-medium text-white disabled:opacity-50 dark:bg-white dark:text-neutral-900"
        >
          {submitting ? "Saving…" : editing ? "Save changes" : "Create product"}
        </button>
        {editing && (
          <button
            type="button"
            onClick={onCancelEdit}
            className="rounded-md border border-neutral-300 px-4 py-1.5 text-sm dark:border-neutral-700"
          >
            Cancel
          </button>
        )}
      </div>
    </form>
  );
}
