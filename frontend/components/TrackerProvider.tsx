"use client";
/**
 * Boots the behavioral tracker exactly once for the whole client tree (PRD §6.3). Mounting this near
 * the root spawns the Web Worker, replays any localStorage spillover from a previous load, and wires
 * scroll/lifecycle listeners. Individual pages then call `tracker.trackView(...)`,
 * `tracker.trackSearch(...)`, `tracker.observeDwell(...)`, etc.
 */

import { useEffect } from "react";

import { tracker } from "../lib/tracker";

export function TrackerProvider({ children }: { children: React.ReactNode }): React.ReactElement {
  useEffect(() => {
    tracker.init();
    // The tracker is a process-wide singleton; no teardown on route changes (only on logout).
  }, []);

  return <>{children}</>;
}
