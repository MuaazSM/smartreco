import { useEffect, useRef } from "react";

/**
 * Runs `callback` on an interval while the tab is visible; pauses on `visibilitychange → hidden` and
 * resumes on refocus, mirroring the tracker's own foreground-only ethos (PRD §6.3). Shared by the
 * dashboard (polling for the U4 "recommendation changed" affordance) and the admin sync-status panel.
 */
export function useVisiblePolling(callback: () => void, intervalMs: number, enabled: boolean): void {
  const savedCallback = useRef(callback);
  savedCallback.current = callback;

  useEffect(() => {
    if (!enabled) return;
    let id: ReturnType<typeof setInterval> | null = null;
    const start = (): void => {
      if (id) return;
      id = setInterval(() => savedCallback.current(), intervalMs);
    };
    const stop = (): void => {
      if (id) clearInterval(id);
      id = null;
    };
    const onVisibility = (): void => {
      if (document.visibilityState === "visible") start();
      else stop();
    };

    if (document.visibilityState === "visible") start();
    document.addEventListener("visibilitychange", onVisibility);
    return () => {
      stop();
      document.removeEventListener("visibilitychange", onVisibility);
    };
  }, [intervalMs, enabled]);
}
