"use client";
/**
 * The dashboard's centerpiece and the product's signature: a recommendation that shows its work.
 * The headline + narrative sit above a list of picks; each pick carries a superscript citation, and
 * the footnotes below spell out the behavior it was grounded in (`item.reason`) plus the real
 * catalog id — proving it isn't invented. Hovering a pick highlights the footnote it's grounded in.
 * This is the live equivalent of the landing hero's static artifact.
 */

import Link from "next/link";
import { useState } from "react";

import { formatRelativeTime } from "../../lib/time";
import { tracker } from "../../lib/tracker";
import type { CurrentRecommendationOut } from "../../lib/types";
import { FeedbackButtons } from "./FeedbackButtons";

function formatPrice(cents: number): string {
  return cents <= 0 ? "Free" : `$${(cents / 100).toFixed(2)}`;
}

function shortId(id: string): string {
  return id.length > 8 ? `${id.slice(0, 8)}…` : id;
}

export function AnnotatedRecommendation({
  rec,
  userName,
}: {
  rec: CurrentRecommendationOut;
  userName: string;
}): React.ReactElement {
  const [hot, setHot] = useState<number | null>(null);

  return (
    <aside className="card shadow-artifact p-6 sm:p-7">
      <div className="flex items-center justify-between gap-3">
        <span className="eyebrow">Recommendation · {userName}</span>
        <span className="mono inline-flex items-center gap-1.5 text-[0.66rem] text-ok">
          <span className="h-1.5 w-1.5 rounded-full bg-ok" aria-hidden />
          updated {formatRelativeTime(rec.transparency.last_generated_at)}
        </span>
      </div>

      <h2 className="mt-3 text-[1.6rem] leading-[1.12] sm:text-[1.9rem]">{rec.headline}</h2>
      <p className="mt-2 text-muted">{rec.narrative}</p>

      <div className="mt-4">
        {rec.items.map((item, i) => (
          <div
            key={item.product_id}
            className="flex items-start justify-between gap-4 border-t border-hairline py-3.5"
            onMouseEnter={() => setHot(i + 1)}
            onMouseLeave={() => setHot(null)}
          >
            <div className="min-w-0">
              <Link
                href={`/catalog/${item.product_id}`}
                onClick={() => tracker.trackClick(item.product_id, { source: "dashboard_card" })}
                className="rec-t transition-colors hover:text-accent"
              >
                {item.title || item.product_id}
                <sup>{i + 1}</sup>
              </Link>
              <div className="mt-1.5 flex flex-wrap items-center gap-1.5">
                {item.category && <span className="tag">{item.category}</span>}
                {item.level && <span className="tag">{item.level}</span>}
              </div>
            </div>
            <div className="flex shrink-0 items-center gap-3">
              <span className="rec-p">{formatPrice(item.price_cents)}</span>
              <FeedbackButtons recId={rec.recommendation_id} itemId={item.product_id} />
            </div>
          </div>
        ))}
      </div>

      <div className="fns">
        {rec.items.map((item, i) => (
          <p key={item.product_id} className={`fn${hot === i + 1 ? " hot" : ""}`}>
            <b>{i + 1}</b>&nbsp;&nbsp;grounded in — {item.reason} · id{" "}
            <span className="ok">
              {shortId(item.product_id)} ✓ in catalog
            </span>
          </p>
        ))}
      </div>
    </aside>
  );
}
