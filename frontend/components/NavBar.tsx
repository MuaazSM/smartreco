"use client";
/**
 * Top navigation. Client component because it reads live auth state (`useAuth`) to decide which
 * links to show, and `usePathname` to switch between the marketing anchors (on the landing page,
 * for anonymous visitors) and the app links (Catalog / Dashboard / Admin) everywhere else.
 */

import Link from "next/link";
import { usePathname } from "next/navigation";

import { useAuth } from "./AuthProvider";
import { ThemeToggle } from "./ui/ThemeToggle";

const appLinkClass = (active: boolean): string =>
  `rounded-md px-3 py-1.5 text-[0.92rem] font-medium transition-colors ${
    active ? "text-accent" : "text-muted hover:text-ink"
  }`;

export function NavBar(): React.ReactElement {
  const { user, loading, logout } = useAuth();
  const pathname = usePathname();

  // Marketing anchors belong on the landing page for signed-out visitors; a signed-in user always
  // gets their working app links so the product is one click away from anywhere.
  const showAnchors = pathname === "/" && !user;

  return (
    <header
      className="sticky top-0 z-40 border-b border-hairline backdrop-blur-[10px]"
      style={{ background: "color-mix(in srgb, var(--paper) 84%, transparent)" }}
    >
      <div className="mx-auto flex h-16 w-full max-w-page items-center justify-between px-7">
        <Link href="/" className="font-display text-[1.4rem] font-semibold tracking-[-0.03em]">
          Smart<b className="font-semibold text-accent">Reco</b>
        </Link>

        {showAnchors ? (
          <nav className="hidden items-center gap-[26px] md:flex">
            <a
              href="#problem"
              className="font-mono text-[0.78rem] lowercase tracking-[0.06em] text-muted transition-colors hover:text-ink"
            >
              the problem
            </a>
            <a
              href="#how"
              className="font-mono text-[0.78rem] lowercase tracking-[0.06em] text-muted transition-colors hover:text-ink"
            >
              how it works
            </a>
            <a
              href="#proof"
              className="font-mono text-[0.78rem] lowercase tracking-[0.06em] text-muted transition-colors hover:text-ink"
            >
              the receipts
            </a>
          </nav>
        ) : (
          <nav className="hidden items-center gap-1 sm:flex">
            <Link href="/catalog" className={appLinkClass(pathname?.startsWith("/catalog") ?? false)}>
              Catalog
            </Link>
            {user && (
              <Link
                href="/dashboard"
                className={appLinkClass(pathname?.startsWith("/dashboard") ?? false)}
              >
                Dashboard
              </Link>
            )}
            {user?.role === "admin" && (
              <Link href="/admin" className={appLinkClass(pathname?.startsWith("/admin") ?? false)}>
                Admin
              </Link>
            )}
          </nav>
        )}

        <div className="flex items-center gap-3">
          <ThemeToggle />
          {loading ? (
            <span className="h-9 w-20 animate-pulse rounded-md bg-hairline" />
          ) : user ? (
            <div className="flex items-center gap-3">
              <span className="hidden text-[0.92rem] text-muted sm:inline">{user.display_name}</span>
              <button type="button" onClick={logout} className="btn btn-o">
                Log out
              </button>
            </div>
          ) : (
            <div className="flex items-center gap-3">
              <Link
                href="/login"
                className="hidden text-[0.92rem] text-muted transition-colors hover:text-ink sm:inline"
              >
                Log in
              </Link>
              <Link href="/register" className="btn">
                Get started
              </Link>
            </div>
          )}
        </div>
      </div>
    </header>
  );
}
