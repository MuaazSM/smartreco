"use client";
/**
 * Landing-page call to action. Client component because the secondary CTA depends on auth state:
 * a logged-out visitor sees "Log in" (the dashboard isn't reachable without an account), while a
 * logged-in user sees "See my recommendations" → /dashboard.
 */

import Link from "next/link";

import { useAuth } from "./AuthProvider";

export function HomeCta(): React.ReactElement {
  const { user, loading } = useAuth();

  return (
    <div className="mt-6 flex flex-wrap gap-3">
      <Link
        href="/catalog"
        className="rounded-md bg-neutral-900 px-5 py-2.5 text-sm font-medium text-white dark:bg-white dark:text-neutral-900"
      >
        Browse the catalog
      </Link>
      {!loading &&
        (user ? (
          <Link
            href="/dashboard"
            className="rounded-md border border-neutral-300 px-5 py-2.5 text-sm font-medium dark:border-neutral-700"
          >
            See my recommendations
          </Link>
        ) : (
          <Link
            href="/login"
            className="rounded-md border border-neutral-300 px-5 py-2.5 text-sm font-medium dark:border-neutral-700"
          >
            Log in
          </Link>
        ))}
    </div>
  );
}
