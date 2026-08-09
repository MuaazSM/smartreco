"use client";
/**
 * Login form (PRD §6.1/§8, U1). `POST /api/auth/login` sets the httpOnly JWT cookie server-side; this
 * component just submits credentials with the credentialed fetch client and then refreshes the shared
 * auth context so the nav bar and the rest of the app see the logged-in state immediately.
 */

import { useRouter } from "next/navigation";
import { useState } from "react";

import { apiPost, ApiError } from "../../lib/api";
import type { UserOut } from "../../lib/types";
import { useAuth } from "../AuthProvider";

export function LoginForm(): React.ReactElement {
  const router = useRouter();
  const { refresh } = useAuth();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function onSubmit(e: React.FormEvent): Promise<void> {
    e.preventDefault();
    if (submitting) return;
    setSubmitting(true);
    setError(null);
    try {
      await apiPost<UserOut>("/api/auth/login", { email, password });
      await refresh();
      router.push("/dashboard");
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Could not log in right now.");
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <form onSubmit={onSubmit} className="mt-6 space-y-4">
      <div>
        <label htmlFor="email" className="block text-sm font-medium">
          Email
        </label>
        <input
          id="email"
          type="email"
          required
          autoComplete="email"
          value={email}
          onChange={(e) => setEmail(e.target.value)}
          className="mt-1 w-full rounded-md border border-neutral-300 bg-transparent px-3 py-2 text-sm dark:border-neutral-700"
        />
      </div>
      <div>
        <label htmlFor="password" className="block text-sm font-medium">
          Password
        </label>
        <input
          id="password"
          type="password"
          required
          autoComplete="current-password"
          value={password}
          onChange={(e) => setPassword(e.target.value)}
          className="mt-1 w-full rounded-md border border-neutral-300 bg-transparent px-3 py-2 text-sm dark:border-neutral-700"
        />
      </div>
      {error && <p className="text-sm text-red-600 dark:text-red-400">{error}</p>}
      <button
        type="submit"
        disabled={submitting}
        className="w-full rounded-md bg-neutral-900 px-4 py-2 text-sm font-medium text-white disabled:opacity-50 dark:bg-white dark:text-neutral-900"
      >
        {submitting ? "Logging in…" : "Log in"}
      </button>
      <p className="text-center text-sm text-neutral-500">
        No account?{" "}
        <a href="/register" className="text-accent underline-offset-2 hover:underline">
          Sign up
        </a>
      </p>
    </form>
  );
}
