"use client";
/**
 * Top navigation. Client component because it reads live auth state (`useAuth`) to decide which
 * links to show — everything else on the site can stay a server component.
 */

import Link from "next/link";
import { usePathname } from "next/navigation";

import { useAuth } from "./AuthProvider";

const linkClass = (active: boolean): string =>
  `rounded-md px-3 py-1.5 text-sm font-medium transition-colors ${
    active
      ? "bg-neutral-900 text-white dark:bg-white dark:text-neutral-900"
      : "text-neutral-600 hover:bg-neutral-100 dark:text-neutral-400 dark:hover:bg-neutral-900"
  }`;

export function NavBar(): React.ReactElement {
  const { user, loading, logout } = useAuth();
  const pathname = usePathname();

  return (
    <header className="border-b border-neutral-200 dark:border-neutral-800">
      <nav className="mx-auto flex max-w-5xl items-center justify-between px-6 py-3">
        <Link href="/" className="text-base font-bold tracking-tight">
          SmartReco
        </Link>

        <div className="flex items-center gap-1">
          <Link href="/catalog" className={linkClass(pathname?.startsWith("/catalog") ?? false)}>
            Catalog
          </Link>
          {user && (
            <Link href="/dashboard" className={linkClass(pathname?.startsWith("/dashboard") ?? false)}>
              Dashboard
            </Link>
          )}
          {user?.role === "admin" && (
            <Link href="/admin" className={linkClass(pathname?.startsWith("/admin") ?? false)}>
              Admin
            </Link>
          )}

          {loading ? (
            <span className="ml-2 h-8 w-16 animate-pulse rounded-md bg-neutral-100 dark:bg-neutral-900" />
          ) : user ? (
            <div className="ml-2 flex items-center gap-2">
              <span className="text-sm text-neutral-500">{user.display_name}</span>
              <button
                type="button"
                onClick={logout}
                className="rounded-md border border-neutral-300 px-3 py-1.5 text-sm dark:border-neutral-700"
              >
                Log out
              </button>
            </div>
          ) : (
            <div className="ml-2 flex items-center gap-1">
              <Link href="/login" className={linkClass(pathname === "/login")}>
                Log in
              </Link>
              <Link
                href="/register"
                className="rounded-md bg-neutral-900 px-3 py-1.5 text-sm text-white dark:bg-white dark:text-neutral-900"
              >
                Sign up
              </Link>
            </div>
          )}
        </div>
      </nav>
    </header>
  );
}
