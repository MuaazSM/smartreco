"use client";
/**
 * Client-side auth context (PRD §6.1/§8, U1). Fetches `GET /api/auth/me` once on mount using the
 * credentialed fetch client (`lib/api.ts`) — the httpOnly JWT cookie set by `POST /api/auth/login`
 * does the actual authenticating; this context only mirrors "who is logged in" into React state so
 * the nav bar, the dashboard guard, and the admin guard don't each fetch it independently.
 *
 * There is no `POST /api/auth/logout` route on the backend (this phase is frontend-only and does not
 * add one — see the Phase 8 report-back FOLLOWUPS). `logout()` here clears local UI state and
 * redirects to `/login`; the httpOnly cookie itself remains valid until its 24h expiry. Good enough
 * for a demo where personas are switched by logging into a different account, but it is not a real
 * session revocation.
 */

import { createContext, useCallback, useContext, useEffect, useMemo, useState } from "react";

import { apiGet } from "../lib/api";
import type { UserOut } from "../lib/types";

interface AuthState {
  user: UserOut | null;
  /** True only while the initial `/api/auth/me` probe is in flight. */
  loading: boolean;
  refresh: () => Promise<void>;
  logout: () => void;
}

const AuthContext = createContext<AuthState | null>(null);

export function AuthProvider({ children }: { children: React.ReactNode }): React.ReactElement {
  const [user, setUser] = useState<UserOut | null>(null);
  const [loading, setLoading] = useState(true);

  const refresh = useCallback(async () => {
    try {
      const me = await apiGet<UserOut>("/api/auth/me");
      setUser(me);
    } catch {
      // A 401 means "not logged in" — the expected case for a guest visitor. A network/backend-down
      // error also resolves to "not logged in" for UI purposes; the page that actually needs data
      // (e.g. the dashboard) will show its own error state when it tries a real request.
      setUser(null);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const logout = useCallback(() => {
    setUser(null);
    if (typeof window !== "undefined") window.location.href = "/login";
  }, []);

  const value = useMemo<AuthState>(
    () => ({ user, loading, refresh, logout }),
    [user, loading, refresh, logout],
  );

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth(): AuthState {
  const ctx = useContext(AuthContext);
  if (!ctx) throw new Error("useAuth must be used within an AuthProvider");
  return ctx;
}
