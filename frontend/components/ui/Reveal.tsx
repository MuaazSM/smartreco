"use client";
/**
 * Scroll-reveal wrapper. Adds `.in` when the element scrolls into view (styles live in
 * globals.css under `[data-reveal]`). Respects `prefers-reduced-motion` by revealing
 * immediately, and degrades to visible when IntersectionObserver is unavailable. Reused on
 * the landing page and across the app so the motion vocabulary stays consistent.
 */

import { useEffect, useRef } from "react";

export function Reveal({
  children,
  className = "",
  delay = 0,
  as: Tag = "div",
}: {
  children: React.ReactNode;
  className?: string;
  delay?: number;
  as?: "div" | "section" | "aside" | "li";
}): React.ReactElement {
  const ref = useRef<HTMLElement | null>(null);

  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    const motionOK =
      window.matchMedia("(prefers-reduced-motion: no-preference)").matches &&
      "IntersectionObserver" in window;
    if (!motionOK) {
      el.classList.add("in");
      return;
    }
    const io = new IntersectionObserver(
      (entries) => {
        entries.forEach((entry) => {
          if (entry.isIntersecting) {
            el.classList.add("in");
            io.unobserve(el);
          }
        });
      },
      { threshold: 0.12, rootMargin: "0px 0px -6% 0px" },
    );
    io.observe(el);
    return () => io.disconnect();
  }, []);

  return (
    <Tag
      ref={ref as never}
      data-reveal=""
      className={className}
      style={delay ? { transitionDelay: `${delay}ms` } : undefined}
    >
      {children}
    </Tag>
  );
}
