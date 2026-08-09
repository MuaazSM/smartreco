"use client";
/**
 * The landing hero's signature element: an annotated recommendation. Each pick carries a
 * superscript citation; hovering a pick highlights the footnote it is grounded in — the same
 * "shows its work" motif the real /dashboard renders from live data. Illustrative content
 * (the "Priya" example) — deliberately static, this is the marketing page.
 */

import { useState } from "react";

export function HeroArtifact(): React.ReactElement {
  const [hot, setHot] = useState<number | null>(null);

  return (
    <aside className="card shadow-artifact p-6">
      <div className="flex items-center justify-between">
        <span className="eyebrow">Recommendation · Priya</span>
        <span className="mono inline-flex items-center gap-1.5 text-[0.66rem] text-ok">
          <span className="h-1.5 w-1.5 rounded-full bg-ok" aria-hidden />
          updated 2m ago
        </span>
      </div>

      <h3 className="mt-3 text-[1.5rem] leading-[1.12]">Your next step in production RAG</h3>
      <p className="mt-1 text-[0.94rem] text-muted">
        You keep returning to advanced retrieval and running vector-search queries. You have the
        fundamentals — here&rsquo;s how to ship it.
      </p>

      <div
        className="rec cursor-default"
        onMouseEnter={() => setHot(1)}
        onMouseLeave={() => setHot(null)}
      >
        <span className="rec-t">
          Hybrid Retrieval &amp; Re-ranking<sup>1</sup>
        </span>
        <span className="rec-p">$119</span>
      </div>
      <div
        className="rec cursor-default"
        onMouseEnter={() => setHot(2)}
        onMouseLeave={() => setHot(null)}
      >
        <span className="rec-t">
          Evaluating RAG Systems<sup>2</sup>
        </span>
        <span className="rec-p">$89</span>
      </div>

      <div className="fns">
        <p className={`fn${hot === 1 ? " hot" : ""}`}>
          <b>1</b>&nbsp;&nbsp;grounded in — opened <em>Advanced RAG</em> 3× this week · 2 vector-search
          queries · 4m dwell · id <span className="ok">c_8241 ✓ active</span>
        </p>
        <p className={`fn${hot === 2 ? " hot" : ""}`}>
          <b>2</b>&nbsp;&nbsp;grounded in — searched <em>&ldquo;rag evaluation&rdquo;</em> · finished{" "}
          <em>Intro to Embeddings</em> · id <span className="ok">c_0417 ✓ active</span>
        </p>
      </div>
    </aside>
  );
}
