"use client";
/**
 * Light/dark toggle. Writes an explicit `data-theme` on <html> (which wins over the OS
 * `prefers-color-scheme` in globals.css) and persists the choice to localStorage, read back
 * by the no-flash init script in `app/layout.tsx`. Renders a stable placeholder glyph until
 * mounted so server and first client render agree (no hydration mismatch).
 */

import { useEffect, useState } from "react";

type Mode = "light" | "dark";

function resolveMode(): Mode {
  const attr = document.documentElement.getAttribute("data-theme");
  if (attr === "light" || attr === "dark") return attr;
  return window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
}

export function ThemeToggle(): React.ReactElement {
  const [mode, setMode] = useState<Mode>("light");
  const [mounted, setMounted] = useState(false);

  useEffect(() => {
    setMounted(true);
    setMode(resolveMode());
  }, []);

  function toggle(): void {
    const next: Mode = resolveMode() === "dark" ? "light" : "dark";
    document.documentElement.setAttribute("data-theme", next);
    try {
      localStorage.setItem("theme", next);
    } catch {
      /* private mode / storage disabled — the toggle still works for this session */
    }
    setMode(next);
  }

  // Show the icon for the current theme (sun in dark, moon in light); before mount render a
  // neutral half-disc so server and client markup match.
  const showSun = mounted && mode === "dark";
  const showMoon = mounted && mode === "light";

  return (
    <button
      type="button"
      onClick={toggle}
      className="tog"
      aria-label={mounted ? `Switch to ${mode === "dark" ? "light" : "dark"} theme` : "Toggle theme"}
      title="Toggle theme"
    >
      <svg width="16" height="16" viewBox="0 0 24 24" fill="none" aria-hidden focusable="false">
        {showMoon ? (
          <path
            d="M20 14.5A8 8 0 1 1 9.5 4a6.5 6.5 0 0 0 10.5 10.5Z"
            fill="currentColor"
          />
        ) : showSun ? (
          <g stroke="currentColor" strokeWidth="1.6" strokeLinecap="round">
            <circle cx="12" cy="12" r="4" fill="currentColor" stroke="none" />
            <path d="M12 2.5v2.2M12 19.3v2.2M2.5 12h2.2M19.3 12h2.2M5 5l1.6 1.6M17.4 17.4 19 19M19 5l-1.6 1.6M6.6 17.4 5 19" />
          </g>
        ) : (
          <path d="M12 3a9 9 0 0 0 0 18Z" fill="currentColor" />
        )}
      </svg>
    </button>
  );
}
