/**
 * Shared fetch client for the SmartReco backend (PRD §8 API surface, §13.2 cookie topology).
 *
 * Same-origin by default. `next.config.mjs` rewrites `/api/*` and `/health` to the FastAPI backend
 * (`BACKEND_ORIGIN`, default `http://localhost:8000`) *inside* the Next.js server process. From the
 * browser's point of view a relative request like `fetch("/api/auth/me")` never leaves the Next
 * origin, so `credentials: "include"` reliably carries the httpOnly `access_token` cookie without any
 * `SameSite=None` dance — this is the "same origin (preferred)" resolution in PRD §13.2, not the
 * cross-origin one.
 *
 * `NEXT_PUBLIC_API_BASE` is an escape hatch for a genuinely split deployment (frontend and backend on
 * different hosts/domains). When set, client-side requests go straight to that origin instead of
 * through the rewrite — the backend must then run with `COOKIE_SAMESITE=none` (Secure) and a
 * credentialed CORS allow-list, or auth will silently fail (see FOLLOWUPS in the Phase 8 report-back).
 *
 * Server Components have no browser cookie jar, so a relative URL can't be resolved and an
 * authenticated request has nothing to send. This module therefore only targets the backend directly
 * (`BACKEND_ORIGIN`) when running on the server, and callers must stick to *public* endpoints (the
 * catalog) from server-side code. Anything that needs the logged-in user's identity — the dashboard,
 * admin, feedback — is fetched from a Client Component instead, where the browser owns the cookie.
 */

const CLIENT_BASE = process.env.NEXT_PUBLIC_API_BASE ?? "";
const SERVER_BASE = process.env.BACKEND_ORIGIN ?? "http://localhost:8000";

function resolveBase(): string {
  return typeof window === "undefined" ? SERVER_BASE : CLIENT_BASE;
}

export class ApiError extends Error {
  readonly status: number;
  readonly detail: unknown;

  constructor(status: number, message: string, detail?: unknown) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.detail = detail;
  }
}

function extractDetailMessage(detail: unknown): string | null {
  if (detail && typeof detail === "object" && "detail" in detail) {
    const inner = (detail as { detail?: unknown }).detail;
    if (typeof inner === "string") return inner;
  }
  return null;
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${resolveBase()}${path}`, {
    ...init,
    credentials: "include",
    cache: init?.cache ?? "no-store",
    headers: {
      "Content-Type": "application/json",
      ...(init?.headers ?? {}),
    },
  });

  if (!res.ok) {
    let detail: unknown;
    try {
      detail = await res.json();
    } catch {
      detail = undefined;
    }
    const message = extractDetailMessage(detail) ?? res.statusText ?? `Request failed (${res.status})`;
    throw new ApiError(res.status, message, detail);
  }

  if (res.status === 204) return undefined as T;
  const text = await res.text();
  return (text ? JSON.parse(text) : undefined) as T;
}

export function apiGet<T>(path: string, init?: RequestInit): Promise<T> {
  return request<T>(path, { ...init, method: "GET" });
}

export function apiPost<T>(path: string, body?: unknown, init?: RequestInit): Promise<T> {
  return request<T>(path, {
    ...init,
    method: "POST",
    body: body !== undefined ? JSON.stringify(body) : undefined,
  });
}

export function apiPatch<T>(path: string, body?: unknown, init?: RequestInit): Promise<T> {
  return request<T>(path, {
    ...init,
    method: "PATCH",
    body: body !== undefined ? JSON.stringify(body) : undefined,
  });
}

export function apiDelete<T>(path: string, init?: RequestInit): Promise<T> {
  return request<T>(path, { ...init, method: "DELETE" });
}
