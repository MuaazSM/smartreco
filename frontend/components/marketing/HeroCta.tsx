"use client";
/**
 * Landing hero call-to-action. Auth-aware: a signed-in visitor is sent straight to their live
 * dashboard; everyone else to sign-up. The secondary link scrolls to "how it works".
 */

import Link from "next/link";

import { useAuth } from "../AuthProvider";

export function HeroCta(): React.ReactElement {
  const { user, loading } = useAuth();
  const primaryHref = user ? "/dashboard" : "/register";
  const primaryLabel = user ? "See my recommendations" : "Get started";

  return (
    <div className="mt-8 flex flex-wrap items-center gap-5">
      <Link className="btn btn-lg" href={loading ? "/register" : primaryHref}>
        {loading ? "Get started" : primaryLabel}
      </Link>
      <Link href="#how" className="inline-flex items-center gap-2 text-[0.98rem] text-ink">
        Read how it works <span className="text-accent">→</span>
      </Link>
    </div>
  );
}
