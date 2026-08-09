# SmartReco — Recommendation Evaluation Results

_Generated 2026-08-09 01:54 UTC · mode: **MESH_DISABLED=true (offline fixtures)** · personas scored: **10/10** · active catalog rows: **124**_

Produced by `evals/eval_recommendations.py` over `evals/dataset.json`. Each persona seeds real behavioral events on real catalog rows, rebuilds the decayed interest profile, runs the **real** seven-node LangGraph agent (`app.agent.run_agent`), and scores the grounded recommendation it returns. Every AI/judge call is routed through the single Mesh gateway (`app/llm/mesh.py`) — no other AI client exists in the repo (invariants #1/#7).

## Aggregate scorecard

| Metric | Definition | Target | Measured | Verdict | Meaningful this run? |
|---|---|---|---:|:---:|:---:|
| Groundedness | recommended products that exist & are active | 100% | **100.0%** | PASS | Yes (structural) |
| Behavioral relevance | item categories in profile's top categories | >= 0.70 | **0.30**† | N/A† | Mechanics only; semantics need real embeddings† |
| Persuasion quality | LLM-as-judge (1-5) via Mesh | >= 4.0 | **4.28*** | N/A* | No (needs live Mesh)* |
| Diversity | unique categories per rec set | >= 2 | **2.80** | PASS | Yes (structural) |

Groundedness and diversity are structural and fully meaningful in every mode. Behavioral relevance and persuasion depend on real Mesh embeddings / a real judge and are only fully meaningful with live Mesh (see the notes below). The harness measures all four correctly regardless — the caveats are about this environment, not the code.

† **Behavioral relevance is not semantically meaningful in this offline run.** It depends on vector retrieval, but the catalog in this environment was seeded under `MESH_DISABLED=true`, so Qdrant holds deterministic **pseudo-random** vectors (verified: within-category cosine ~= cross-category cosine ~= 0, unit norm). Vector similarity is therefore not semantic here, so the retriever's category signal comes mostly from the real BM25/keyword leg over the persona-independent offline `plan_queries` fixture rather than from true interest matching — which is why the measured overlap is low and does NOT reflect the recommender's real quality. To get a true number: fund a Mesh balance, re-seed the catalog with live embeddings, and rerun this harness with live Mesh.

\* **Persuasion is fixture-derived in this run.** Under `MESH_DISABLED=true` the `generate` node emits a fixture narrative and the judge call (routed through Mesh) returns the `grade_retrieval` fixture's 0-1 self-grade, rescaled here to the 1-5 band for a consistent column. It is **not** a real persuasion judgment — rerun without `MESH_DISABLED` for a true score.

## Per-persona results

| Persona | Profile top cats | Items | Grounded | Relevance | Diversity | Persuasion | Fallback |
|---|---|---:|:---:|---:|---:|---:|:---:|
| `p01` Data & ML Foundations | Data Science, Machine Learning | 3 | 100% (3/3) | 0.67 (2/3) | 3 (Data Science, Machine Learning, Programming) | 4.28* | no |
| `p02` Applied AI Engineer | AI & LLMs, Machine Learning | 3 | 100% (3/3) | 0.00 (0/3) | 2 (Data Science, Programming) | 4.28* | no |
| `p03` Frontend Developer | Web Development, Programming | 3 | 100% (3/3) | 0.33 (1/3) | 3 (Data Science, Machine Learning, Programming) | 4.28* | no |
| `p04` Cloud & DevOps Practitioner | Cloud & DevOps, Programming | 3 | 100% (3/3) | 0.33 (1/3) | 3 (Data Science, Machine Learning, Programming) | 4.28* | no |
| `p05` Security & Reliability | Cloud & DevOps, Cybersecurity | 3 | 100% (3/3) | 0.00 (0/3) | 3 (Data Science, Machine Learning, Programming) | 4.28* | no |
| `p06` Analytics & Visualization | Data Science, Web Development | 3 | 100% (3/3) | 0.33 (1/3) | 3 (Data Science, Machine Learning, Programming) | 4.28* | no |
| `p07` Deep Learning Researcher | Machine Learning, AI & LLMs | 3 | 100% (3/3) | 1.00 (3/3) | 2 (AI & LLMs, Machine Learning) | 4.28* | no |
| `p08` Full-Stack Builder | Web Development, Cloud & DevOps | 3 | 100% (3/3) | 0.00 (0/3) | 3 (Data Science, Machine Learning, Programming) | 4.28* | no |
| `p09` Systems Programmer | Programming, Cloud & DevOps | 3 | 100% (3/3) | 0.00 (0/3) | 3 (AI & LLMs, Data Science, Machine Learning) | 4.28* | no |
| `p10` LLM Product Builder | AI & LLMs, Data Science | 3 | 100% (3/3) | 0.33 (1/3) | 3 (AI & LLMs, Machine Learning, Web Development) | 4.28* | no |

## Grounding cross-check (invariant #2)

Every recommended `product_id` across every persona resolves to an **active** catalog row. Groundedness is 100% by independent verification, not by trusting the agent.

## Agent execution (the graph is real, not decorative)

| Persona | Events seeded | Profile dim | Retrieval score | Node path |
|---|---:|---:|---:|---|
| `p01` | 28 | 1536 | 0.82 | build_profile -> plan_queries -> retrieve -> grade_retrieval -> generate -> validate_grounding |
| `p02` | 28 | 1536 | 0.82 | build_profile -> plan_queries -> retrieve -> grade_retrieval -> generate -> validate_grounding |
| `p03` | 28 | 1536 | 0.82 | build_profile -> plan_queries -> retrieve -> grade_retrieval -> generate -> validate_grounding |
| `p04` | 28 | 1536 | 0.82 | build_profile -> plan_queries -> retrieve -> grade_retrieval -> generate -> validate_grounding |
| `p05` | 16 | 1536 | 0.82 | build_profile -> plan_queries -> retrieve -> grade_retrieval -> generate -> validate_grounding |
| `p06` | 28 | 1536 | 0.82 | build_profile -> plan_queries -> retrieve -> grade_retrieval -> generate -> validate_grounding |
| `p07` | 28 | 1536 | 0.82 | build_profile -> plan_queries -> retrieve -> grade_retrieval -> generate -> validate_grounding |
| `p08` | 28 | 1536 | 0.82 | build_profile -> plan_queries -> retrieve -> grade_retrieval -> generate -> validate_grounding |
| `p09` | 20 | 1536 | 0.82 | build_profile -> plan_queries -> retrieve -> grade_retrieval -> generate -> validate_grounding |
| `p10` | 28 | 1536 | 0.82 | build_profile -> plan_queries -> retrieve -> grade_retrieval -> generate -> validate_grounding |

## Method & metric definitions

- **Groundedness** = (# recommended items whose `product_id` exists and `is_active`) / (# items), verified with a direct catalog query per item. Target 100%.
- **Behavioral relevance** = (# items whose category is a key of the rebuilt profile's `top_categories`) / (# items). Uses the behavior-derived profile, not the persona's declared intent. Target >= 0.70.
- **Persuasion quality** = mean of a 1-5 LLM-as-judge score (specificity + reference to real behavior) obtained via `mesh.complete_structured(node="grade_retrieval", tier="cheap")` — the only sanctioned AI path. Target >= 4.0. Needs live Mesh to be meaningful (see note).
- **Diversity** = count of unique categories in the recommendation set. Target >= 2.
- Recommendations exclude products the learner already engaged with, so every scored item is a genuinely new suggestion. The offline `plan_queries` fixture also pins a `level=advanced` retrieval filter, which does not affect the category-based relevance/diversity metrics.
