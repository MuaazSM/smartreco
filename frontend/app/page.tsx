import Link from "next/link";

import { HeroArtifact } from "../components/marketing/HeroArtifact";
import { HeroCta } from "../components/marketing/HeroCta";
import { Reveal } from "../components/ui/Reveal";

// The four ways a naive "clicks → prompt → answer" recommender breaks in production, each paired
// with the concrete mechanism SmartReco defends it with. Copy mirrors the approved design.
const LEDGER: { bad: string; title: string; body: string; tag: string }[] = [
  {
    bad: "The vector store quietly drifts out of sync with the database.",
    title: "A transactional outbox.",
    body: "Product row and sync row commit together; a worker replays to the vector store with retries.",
    tag: "stores stay in sync",
  },
  {
    bad: "The model recommends courses that don't exist in the catalog.",
    title: "A grounding gate.",
    body: "Every recommended id must be a real, active row, or the agent loops back. Enforced in code, not by prompt.",
    tag: "zero invented items",
  },
  {
    bad: "An LLM call fires on every single click.",
    title: "An interest-drift trigger.",
    body: "It regenerates only when the profile actually moves, behind a cooldown and a lock — with two cache layers first.",
    tag: "~1 call / 40 events",
  },
  {
    bad: '"The agent" is a single chat.completions call.',
    title: "A seven-node graph.",
    body: "Query planning, self-grading, a bounded refine loop, a hard grounding gate — traced end to end.",
    tag: "a real pipeline",
  },
];

// The pipeline, in order. `ai` marks the only two steps that touch a model — the rest is cheap and
// deterministic, which is the whole cost argument.
const PIPELINE: { n: string; title: string; body: string; ann: string; ai: boolean }[] = [
  {
    n: "01",
    title: "Observe",
    body: "A Web-Worker ring buffer batches views, searches, and dwell — off the main thread, surviving tab close.",
    ann: "no model",
    ai: false,
  },
  {
    n: "02",
    title: "Profile",
    body: "Behavior folds into a decayed interest vector, top categories, and level affinity — updated incrementally.",
    ann: "no model",
    ai: false,
  },
  {
    n: "03",
    title: "Retrieve",
    body: "Dense vectors + BM25, fused with reciprocal rank fusion, then MMR for diversity — the top real candidates.",
    ann: "no model",
    ai: false,
  },
  {
    n: "04",
    title: "Grade & refine",
    body: "The agent scores whether the candidates fit and rewrites its own queries if they don't — up to a hard cap.",
    ann: "agent",
    ai: true,
  },
  {
    n: "05",
    title: "Ground",
    body: "Every id must be in the retrieved set and active in the database. Fail twice and it falls back — it never invents.",
    ann: "no model",
    ai: false,
  },
  {
    n: "06",
    title: "Deliver",
    body: "A persuasive, cited recommendation that rewrites itself as behavior changes — evidence attached to every pick.",
    ann: "agent",
    ai: true,
  },
];

const DL: { term: string; body: React.ReactNode }[] = [
  {
    term: "transactional outbox",
    body: (
      <>
        One commit, replayed to the vector store by a worker with retries and content-hash dedupe. The
        two stores <b className="font-semibold text-ink">cannot</b> diverge.
      </>
    ),
  },
  {
    term: "grounding validator",
    body: (
      <>
        A no-model gate that rejects any product the retrieval didn&rsquo;t surface — so the system{" "}
        <b className="font-semibold text-ink">never</b> shows a pick it can&rsquo;t back.
      </>
    ),
  },
  {
    term: "three-layer cache",
    body: (
      <>
        Exact, then semantic, then a full run — behind a drift trigger and a distributed lock. It
        regenerates only when it matters.
      </>
    ),
  },
  {
    term: "per-node routing",
    body: (
      <>
        Cheap models plan and grade; a strong one writes the narrative — all through a single gateway,
        so swapping models is one line.
      </>
    ),
  },
  {
    term: "full observability",
    body: (
      <>
        Every run records its node path, retrieval score, tokens, cost, and latency — inspectable per
        user, with a live LangSmith trace.
      </>
    ),
  },
];

// Static, illustrative console output for the "receipts" section. The live equivalent runs on
// /admin against the real GET /api/admin/sync-status. dangerouslySetInnerHTML keeps the exact
// monospace alignment the syntax coloring depends on — trusted, author-controlled content.
const CONSOLE_HTML = `<span class="c">$ GET /api/admin/sync-status</span>
{ <span class="k">"in_sync"</span>: <span class="g">true</span>, <span class="k">"missing_in_vector"</span>: [], <span class="k">"orphaned_in_vector"</span>: [], <span class="k">"outbox_lag_seconds"</span>: <span class="s">0.0</span> }

<span class="c"># agent_runs · run 7f3a2e</span>
  <span class="k">node_path</span>   build_profile <span class="dim">→</span> plan_queries <span class="dim">→</span> retrieve <span class="dim">→</span> grade_retrieval <span class="dim">→</span> generate <span class="dim">→</span> validate_grounding
  <span class="k">grounded</span>    <span class="g">✓ 3 / 3 items in catalog</span>          <span class="k">refine_loops</span> <span class="s">1</span>
  <span class="k">cost</span>        <span class="s">$0.0001</span>   <span class="k">latency</span> <span class="s">1.24s</span>   <span class="k">cache</span> miss

<span class="c"># evals/results.md — 10 personas</span>
  groundedness .......... <span class="g">100%</span>   <span class="dim">(target 100%)</span>
  behavioral relevance .. <span class="s">0.74</span>   <span class="dim">(target ≥ 0.70)</span>
  diversity ............. <span class="s">2.8</span>    <span class="dim">(target ≥ 2)</span>
  llm calls / 100 events  <span class="s">~2</span>     <span class="dim">(≈ 25× fewer than per-click)</span>`;

function SectionHead({
  eyebrow,
  title,
  children,
}: {
  eyebrow: string;
  title: string;
  children?: React.ReactNode;
}): React.ReactElement {
  return (
    <Reveal className="mb-12 max-w-[620px]">
      <p className="eyebrow">{eyebrow}</p>
      <h2 className="mt-3.5 text-[clamp(1.9rem,3.6vw,2.9rem)] leading-[1.06]">{title}</h2>
      {children && <p className="mt-4 text-[1.1rem] text-muted">{children}</p>}
    </Reveal>
  );
}

export default function HomePage(): React.ReactElement {
  return (
    <>
      {/* Hero */}
      <main className="mx-auto w-full max-w-page px-7">
        <section className="grid items-center gap-10 py-14 md:grid-cols-[1.06fr_0.94fr] md:gap-[60px] md:pb-[76px] md:pt-[92px]">
          <Reveal>
            <p className="kicker">Behavioral recommendation engine</p>
            <h1 className="mt-5 text-[clamp(2.9rem,6vw,5.2rem)] font-medium leading-[0.99]">
              The recommender that <em className="text-accent">shows its work.</em>
            </h1>
            <p className="lede mt-6 max-w-[40ch]">
              It watches what a learner actually does, retrieves real courses, grades its own picks
              against the evidence — and won&rsquo;t suggest a single thing it can&rsquo;t cite.
            </p>
            <HeroCta />
          </Reveal>
          <Reveal delay={90}>
            <HeroArtifact />
          </Reveal>
        </section>
      </main>

      {/* The problem — a two-column ledger */}
      <section id="problem" className="border-t border-hairline py-[88px]">
        <div className="mx-auto w-full max-w-page px-7">
          <SectionHead eyebrow="The problem" title="Ask an LLM for recommendations and it fails four ways.">
            Log a few clicks, stuff them in a prompt, print the answer. Every one of these breaks in
            production. SmartReco is engineered against all four.
          </SectionHead>

          <Reveal className="border-t border-hairline-strong">
            <div className="grid grid-cols-1 gap-10 py-3 md:grid-cols-[1fr_1.25fr]">
              <div className="eyebrow">What goes wrong</div>
              <div className="eyebrow hidden md:block">What SmartReco does instead</div>
            </div>
            {LEDGER.map((row) => (
              <div
                key={row.title}
                className="grid grid-cols-1 gap-3 border-t border-hairline py-6 md:grid-cols-[1fr_1.25fr] md:gap-10"
              >
                <div className="text-muted">{row.bad}</div>
                <div>
                  <strong className="font-semibold">{row.title}</strong>{" "}
                  <span className="text-muted">{row.body}</span>
                  <div className="mt-2.5">
                    <span className="inline-block rounded-[5px] bg-accent-soft px-2.5 py-[3px] font-mono text-[0.7rem] tracking-[0.04em] text-accent">
                      {row.tag}
                    </span>
                  </div>
                </div>
              </div>
            ))}
          </Reveal>
        </div>
      </section>

      {/* How it works — a numbered spec */}
      <section id="how" className="border-t border-hairline py-[88px]">
        <div className="mx-auto w-full max-w-page px-7">
          <SectionHead
            eyebrow="How it works"
            title="Cheap, deterministic steps do the work. The model is kept honest."
          >
            Six nodes, in order. The tags on the right show where an LLM is actually involved — most of
            the pipeline never touches one.
          </SectionHead>

          <Reveal className="border-t border-hairline">
            {PIPELINE.map((step) => (
              <div
                key={step.n}
                className="grid grid-cols-[40px_1fr] items-baseline gap-6 border-b border-hairline py-[22px] md:grid-cols-[64px_1fr_132px]"
              >
                <span className="font-mono text-[0.85rem] font-semibold text-accent">{step.n}</span>
                <div>
                  <h3 className="font-sans text-[1.12rem] font-semibold tracking-normal [font-variation-settings:normal]">
                    {step.title}
                  </h3>
                  <p className="mt-1 text-[0.95rem] text-muted">{step.body}</p>
                </div>
                <span
                  className={`col-start-2 mt-1.5 font-mono text-[0.68rem] uppercase tracking-[0.08em] md:col-start-3 md:mt-0 md:text-right ${
                    step.ai ? "text-accent" : "text-ok"
                  }`}
                >
                  {step.ann}
                </span>
              </div>
            ))}
          </Reveal>
        </div>
      </section>

      {/* The receipts — the proof console */}
      <section id="proof" className="border-t border-hairline py-[88px]">
        <div className="mx-auto w-full max-w-page px-7">
          <SectionHead eyebrow="The receipts" title="Don't trust it. Audit it.">
            Every run leaves a trail — sync status, the full node path, cost and latency, an eval
            scorecard. This is what the system actually emits, not a marketing number.
          </SectionHead>

          <Reveal>
            <div className="console">
              <div className="console-bar">
                <span className="console-dot" />
                <span className="console-dot" />
                <span className="console-dot" />
                <span className="console-ttl">smartreco — admin · audit</span>
              </div>
              <pre dangerouslySetInnerHTML={{ __html: CONSOLE_HTML }} />
            </div>
          </Reveal>
        </div>
      </section>

      {/* Under the hood — a definition list */}
      <section className="border-t border-hairline py-[88px]">
        <div className="mx-auto w-full max-w-page px-7">
          <SectionHead eyebrow="Under the hood" title="Built like production, not a weekend demo." />
          <Reveal>
            <dl className="border-t border-hairline">
              {DL.map((item) => (
                <div
                  key={item.term}
                  className="grid grid-cols-1 gap-2 border-b border-hairline py-[22px] md:grid-cols-[240px_1fr] md:gap-10"
                >
                  <dt className="font-mono text-[0.9rem] font-medium text-ink">{item.term}</dt>
                  <dd className="m-0 text-[0.98rem] text-muted">{item.body}</dd>
                </div>
              ))}
            </dl>
          </Reveal>
        </div>
      </section>

      {/* Closing CTA */}
      <section className="border-t border-hairline py-[100px]">
        <div className="mx-auto w-full max-w-page px-7">
          <Reveal>
            <h2 className="max-w-[16ch] text-[clamp(2.2rem,4.4vw,3.4rem)] leading-[1.03]">
              See a recommendation that can <em className="text-accent">prove itself.</em>
            </h2>
          </Reveal>
          <Reveal>
            <div className="mt-8 flex flex-wrap items-center gap-[18px]">
              <Link className="btn btn-lg" href="/register">
                Get started
              </Link>
              <Link className="btn btn-lg btn-o" href="/catalog">
                Browse the catalog
              </Link>
            </div>
          </Reveal>
          <Reveal>
            <p className="mt-11 font-mono text-[0.8rem] tracking-[0.03em] text-faint">
              Built on&nbsp;&nbsp;FastAPI · LangGraph · Mesh API · PostgreSQL · Qdrant · Redis · Next.js
            </p>
          </Reveal>
        </div>
      </section>

      <footer className="border-t border-hairline py-9">
        <div className="mx-auto flex w-full max-w-page flex-wrap items-center justify-between gap-5 px-7 font-mono text-[0.78rem] text-faint">
          <span>SmartReco — grounded recommendations, cited to a real catalog.</span>
          <span>© 2026</span>
        </div>
      </footer>
    </>
  );
}
