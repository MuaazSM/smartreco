"use client";
/**
 * Auth/role gate for `/admin` (PRD §6.1 `require_role("admin")`, §6.2). Kept separate from
 * `AdminDashboard` so the admin data fetch never fires for a non-admin session — the backend would
 * 403 it anyway, but there is no reason to try.
 */

import { useAuth } from "../AuthProvider";
import { SkeletonBlock } from "../Skeleton";
import { AdminDashboard } from "./AdminDashboard";

export function AdminView(): React.ReactElement {
  const { user, loading } = useAuth();

  if (loading) {
    return (
      <main className="mx-auto max-w-4xl px-6 py-12">
        <SkeletonBlock className="h-8 w-48" />
        <SkeletonBlock className="mt-4 h-24 w-full" />
      </main>
    );
  }

  if (!user) {
    return (
      <main className="mx-auto max-w-4xl px-6 py-12">
        <h1 className="text-2xl font-bold">Admin</h1>
        <p className="mt-3 text-neutral-600 dark:text-neutral-400">
          Log in with an admin account to manage the catalog.
        </p>
        <a
          href="/login"
          className="mt-4 inline-block rounded-md bg-neutral-900 px-4 py-2 text-sm font-medium text-white dark:bg-white dark:text-neutral-900"
        >
          Log in
        </a>
      </main>
    );
  }

  if (user.role !== "admin") {
    return (
      <main className="mx-auto max-w-4xl px-6 py-12">
        <h1 className="text-2xl font-bold">Admin</h1>
        <p className="mt-3 text-neutral-600 dark:text-neutral-400">
          This account does not have admin access.
        </p>
      </main>
    );
  }

  return (
    <main className="mx-auto max-w-4xl px-6 py-12">
      <h1 className="text-2xl font-bold">Admin</h1>
      <p className="mt-2 text-neutral-600 dark:text-neutral-400">
        Product CRUD and the Postgres/Qdrant sync-status readout.
      </p>
      <AdminDashboard />
    </main>
  );
}
