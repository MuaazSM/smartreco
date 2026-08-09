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
      <main className="mx-auto max-w-page px-7 py-12">
        <SkeletonBlock className="h-8 w-48" />
        <SkeletonBlock className="mt-4 h-24 w-full" />
      </main>
    );
  }

  if (!user) {
    return (
      <main className="mx-auto max-w-page px-7 py-12">
        <p className="eyebrow">Admin</p>
        <h1 className="mt-2 text-3xl">Admin access</h1>
        <p className="mt-3 text-muted">Log in with an admin account to manage the catalog.</p>
        <a href="/login" className="btn mt-5">
          Log in
        </a>
      </main>
    );
  }

  if (user.role !== "admin") {
    return (
      <main className="mx-auto max-w-page px-7 py-12">
        <p className="eyebrow">Admin</p>
        <h1 className="mt-2 text-3xl">Admin access</h1>
        <p className="mt-3 text-muted">This account doesn&rsquo;t have admin access.</p>
      </main>
    );
  }

  return (
    <main className="mx-auto max-w-page px-7 py-12">
      <p className="eyebrow">Admin · catalog &amp; sync</p>
      <h1 className="mt-2 text-3xl">Product CRUD, and the receipts</h1>
      <p className="mt-2 text-muted">
        Create and edit courses, and watch Postgres and Qdrant stay in lockstep — the single most
        demonstrable claim in the system.
      </p>
      <AdminDashboard />
    </main>
  );
}
