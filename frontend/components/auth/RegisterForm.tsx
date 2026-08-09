"use client";
/**
 * Registration form (PRD §6.1/§8, U1). `POST /api/auth/register` creates the account but does not set
 * a session cookie, so this immediately follows up with a login call using the same credentials —
 * one submit takes the learner straight to `/dashboard` rather than a second form.
 */

import { useRouter } from "next/navigation";
import { useState } from "react";

import { apiPost, ApiError } from "../../lib/api";
import type { UserOut } from "../../lib/types";
import { useAuth } from "../AuthProvider";

export function RegisterForm(): React.ReactElement {
  const router = useRouter();
  const { refresh } = useAuth();
  const [email, setEmail] = useState("");
  const [displayName, setDisplayName] = useState("");
  const [password, setPassword] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function onSubmit(e: React.FormEvent): Promise<void> {
    e.preventDefault();
    if (submitting) return;
    setSubmitting(true);
    setError(null);
    try {
      await apiPost<UserOut>("/api/auth/register", {
        email,
        password,
        display_name: displayName,
      });
      await apiPost<UserOut>("/api/auth/login", { email, password });
      await refresh();
      router.push("/dashboard");
    } catch (err) {
      if (err instanceof ApiError && err.status === 409) {
        setError("An account with that email already exists.");
      } else {
        setError(err instanceof ApiError ? err.message : "Could not sign up right now.");
      }
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <form onSubmit={onSubmit} className="mt-6 space-y-4">
      <div>
        <label htmlFor="display_name" className="block text-sm font-medium">
          Name
        </label>
        <input
          id="display_name"
          type="text"
          required
          autoComplete="name"
          value={displayName}
          onChange={(e) => setDisplayName(e.target.value)}
          className="mt-1 w-full rounded-md border border-neutral-300 bg-transparent px-3 py-2 text-sm dark:border-neutral-700"
        />
      </div>
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
          minLength={8}
          autoComplete="new-password"
          value={password}
          onChange={(e) => setPassword(e.target.value)}
          className="mt-1 w-full rounded-md border border-neutral-300 bg-transparent px-3 py-2 text-sm dark:border-neutral-700"
        />
        <p className="mt-1 text-xs text-neutral-500 dark:text-neutral-400">At least 8 characters.</p>
      </div>
      {error && <p className="text-sm text-red-600 dark:text-red-400">{error}</p>}
      <button
        type="submit"
        disabled={submitting}
        className="w-full rounded-md bg-neutral-900 px-4 py-2 text-sm font-medium text-white disabled:opacity-50 dark:bg-white dark:text-neutral-900"
      >
        {submitting ? "Creating account…" : "Sign up"}
      </button>
      <p className="text-center text-sm text-neutral-500">
        Already have an account?{" "}
        <a href="/login" className="text-accent underline-offset-2 hover:underline">
          Log in
        </a>
      </p>
    </form>
  );
}
