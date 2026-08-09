"""Grounding-gate invariant tests (PRD §6.4 node 7; CLAUDE.md invariant #2). Keep strong.

The single most important claim in the submission is that the system **never** surfaces an ungrounded
recommendation. These tests exercise the real LangGraph graph end to end with a fake vector store and
``MESH_DISABLED=true`` fixtures — no Postgres, no Qdrant, no network — so they are deterministic and
prove the graph's behavior, not the infrastructure:

  * an out-of-catalog ``product_id`` is **rejected** by ``validate_grounding``; the graph retries once
    and then falls back to the deterministic top-K path, so the emitted items are always grounded;
  * the happy path produces 3-5 grounded items with per-item reasons, valid, no fallback;
  * the refine loop is visible in ``node_path`` and hard-capped at 2 (retrieve runs 3×, refine 2×);
  * the structural gate itself rejects ungrounded ids, duplicates, and under-count drafts directly.

No LLM/embedding client is constructed here — the only model access is the Mesh gateway's offline
fixtures (invariants #1/#7).
"""

from __future__ import annotations

import math
import os
import types
import uuid

# Offline: the graph's Mesh calls resolve to recorded fixtures with zero network I/O.
os.environ.setdefault("MESH_DISABLED", "true")
os.environ.setdefault("SMARTRECO_RUN_SCHEDULER", "false")

import pytest  # noqa: E402

from app.agent.context import AgentContext  # noqa: E402
from app.agent.graph import build_agent_graph, initial_state  # noqa: E402
from app.agent.nodes.validate_grounding import _validate  # noqa: E402
from app.agent.schemas import GradeOutput  # noqa: E402
from app.llm import mesh  # noqa: E402
from app.llm.mesh import MeshResult, MeshUsage  # noqa: E402
from app.services.profile import ProfileSnapshot  # noqa: E402
from app.vector.hybrid_retriever import ProductDoc, RetrievalFilters, hybrid_retrieve  # noqa: E402


# --------------------------------------------------------------------------------------------------
# Fixtures: a small deterministic catalog + a fake read-only vector store
# --------------------------------------------------------------------------------------------------
def _vec(seed: str, dim: int = 8) -> list[float]:
    import hashlib
    import random

    rng = random.Random(hashlib.sha256(seed.encode()).digest())
    values = [rng.gauss(0.0, 1.0) for _ in range(dim)]
    norm = math.sqrt(sum(v * v for v in values)) or 1.0
    return [v / norm for v in values]


def _corpus() -> list[ProductDoc]:
    specs = [
        ("Advanced Retrieval-Augmented Generation", "AI & LLMs", "advanced", ["rag", "reranking"], 12900),
        ("Vector Databases Deep Dive", "AI & LLMs", "intermediate", ["vector-db", "embeddings"], 7900),
        ("LangGraph Agents from Scratch", "AI & LLMs", "advanced", ["agents", "langgraph"], 13900),
        ("Deep Learning with PyTorch", "Machine Learning", "intermediate", ["deep-learning", "pytorch"], 9900),
        ("Model Deployment & MLOps", "Machine Learning", "advanced", ["mlops", "deployment"], 11900),
        ("Feature Engineering Masterclass", "Data Science", "advanced", ["features", "ml"], 8900),
    ]
    return [
        ProductDoc(
            product_id=str(uuid.uuid4()),
            title=title,
            description=f"{title}: hands-on {' '.join(tags)}",
            category=category,
            tags=tags,
            level=level,
            price_cents=price,
        )
        for title, category, level, tags, price in specs
    ]


class _FakeVectorStore:
    """Read-only stand-in for ``QdrantVectorStore`` — returns fixed vectors and a cosine ranking."""

    def __init__(self, corpus: list[ProductDoc]) -> None:
        self._vectors = {doc.product_id: _vec(doc.title) for doc in corpus}
        self._corpus = corpus

    async def retrieve_vectors(self, ids):
        return {str(i): self._vectors[str(i)] for i in ids if str(i) in self._vectors}

    async def search(self, vector, *, limit: int = 12, query_filter=None):
        def cos(a, b):
            if len(a) != len(b):
                return 0.0
            dot = sum(x * y for x, y in zip(a, b))
            na = math.sqrt(sum(x * x for x in a))
            nb = math.sqrt(sum(x * x for x in b))
            return dot / (na * nb) if na and nb else 0.0

        ranked = sorted(self._corpus, key=lambda d: cos(vector, self._vectors[d.product_id]), reverse=True)
        return [types.SimpleNamespace(id=d.product_id, score=1.0, payload={}) for d in ranked[:limit]]

    async def close(self):  # pragma: no cover - nothing to close
        return None


def _make_ctx(corpus: list[ProductDoc], *, remap: bool = True, seen: set[str] | None = None) -> AgentContext:
    centroid = _vec(corpus[0].title)  # near the first (advanced RAG) course
    snapshot = ProfileSnapshot(
        user_id=uuid.uuid4(),
        interest_vector=centroid,
        top_categories={"AI & LLMs": 5.0, "Machine Learning": 2.0},
        top_terms={"rag": 4.0, "pytorch": 2.0},
        level_affinity={"advanced": 4.0, "intermediate": 1.0},
        events_since_gen=10,
        profile_hash="deadbeef",
        considered_events=12,
    )
    evidence = [
        {"type": "search", "query": "rag pipeline", "weight": 2.0},
        {"type": "view", "product_id": corpus[3].product_id, "title": corpus[3].title,
         "category": corpus[3].category, "weight": 1.0},
    ]
    return AgentContext(
        vector_store=_FakeVectorStore(corpus),
        corpus=corpus,
        interest_vector=centroid,
        profile_snapshot=snapshot,
        recent_events=evidence,
        seen_product_ids=set(seen or set()),
        on_usage=None,
        remap_placeholders=remap,
    )


async def _run(ctx: AgentContext, thread: str) -> dict:
    graph = build_agent_graph()
    config = {"configurable": {"ctx": ctx, "thread_id": thread}, "recursion_limit": 40}
    return await graph.ainvoke(initial_state("test-user"), config)


def _grade_stub(score: float):
    """A ``mesh.complete_structured`` wrapper that pins the grade score, delegating other nodes."""
    original = mesh.complete_structured

    async def wrapper(node, messages, response_model, **kwargs):
        if node == "grade_retrieval":
            usage = MeshUsage(
                node=node, model="fake", tier="cheap", prompt_tokens=0, completion_tokens=0,
                total_tokens=0, cost_usd=0.0, latency_ms=0.0, disabled=True,
            )
            return MeshResult(
                data=GradeOutput(score=score, gap="needs broader evaluation coverage", decisions=[]),
                text="", model="fake", usage=usage,
            )
        return await original(node, messages, response_model, **kwargs)

    return original, wrapper


# --------------------------------------------------------------------------------------------------
# Tests — the graph end to end
# --------------------------------------------------------------------------------------------------
async def test_happy_path_produces_3_to_5_grounded_items() -> None:
    corpus = _corpus()
    catalog_ids = {doc.product_id for doc in corpus}
    final = await _run(_make_ctx(corpus, remap=True), "happy")

    assert final["valid"] is True
    assert final.get("fallback_used") is not True
    items = final["draft"]["items"]
    assert 3 <= len(items) <= 5
    # Every item is grounded (in the catalog) and carries a per-item reason.
    assert all(item["product_id"] in catalog_ids for item in items)
    assert all(item["reason"].strip() for item in items)
    # The full seven-node path ran with no refine loop and no fallback.
    assert final["node_path"] == [
        "build_profile", "plan_queries", "retrieve", "grade_retrieval",
        "generate", "validate_grounding",
    ]


async def test_out_of_catalog_id_is_rejected_then_falls_back_grounded() -> None:
    """THE invariant: an ungrounded id is rejected, the graph retries once, then falls back — and
    every emitted item is still grounded. The system never surfaces an ungrounded recommendation."""
    corpus = _corpus()
    catalog_ids = {doc.product_id for doc in corpus}
    # remap=False leaves the fixture's PLACEHOLDER_PRODUCT_* ids ungrounded (not in the catalog).
    final = await _run(_make_ctx(corpus, remap=False), "ungrounded")

    path = final["node_path"]
    # generate was retried once (2 generates, 2 validations) and then the deterministic fallback ran.
    assert path.count("generate") == 2
    assert path.count("validate_grounding") == 2
    assert path[-1] == "fallback"
    assert final.get("fallback_used") is True

    items = final["draft"]["items"]
    assert 3 <= len(items) <= 5
    # The whole point: not one ungrounded item survives.
    assert all(item["product_id"] in catalog_ids for item in items)


async def test_refine_loop_is_visible_and_capped_at_two(monkeypatch: pytest.MonkeyPatch) -> None:
    corpus = _corpus()
    original, wrapper = _grade_stub(score=0.1)  # always below threshold → refine until capped
    monkeypatch.setattr(mesh, "complete_structured", wrapper)

    final = await _run(_make_ctx(corpus, remap=True), "refine")
    monkeypatch.setattr(mesh, "complete_structured", original)

    path = final["node_path"]
    assert final["refine_loops"] == 2
    assert path.count("refine_queries") == 2      # hard cap enforced
    assert path.count("retrieve") == 3            # initial + one per refine loop
    # After the budget is exhausted the graph proceeds to generate and produces grounded items.
    assert path.count("generate") >= 1
    assert 3 <= len(final["draft"]["items"]) <= 5
    assert all(i["product_id"] in {d.product_id for d in corpus} for i in final["draft"]["items"])


# --------------------------------------------------------------------------------------------------
# Tests — the structural gate in isolation (no LLM, pure)
# --------------------------------------------------------------------------------------------------
def test_validate_gate_rejects_ungrounded_and_duplicate_and_undercount() -> None:
    corpus = _corpus()
    active_ids = {doc.product_id for doc in corpus}
    candidates = [
        {"product_id": corpus[0].product_id, "title": corpus[0].title, "category": corpus[0].category,
         "level": corpus[0].level, "price_cents": corpus[0].price_cents, "tags": corpus[0].tags},
        {"product_id": corpus[1].product_id, "title": corpus[1].title, "category": corpus[1].category,
         "level": corpus[1].level, "price_cents": corpus[1].price_cents, "tags": corpus[1].tags},
        {"product_id": corpus[2].product_id, "title": corpus[2].title, "category": corpus[2].category,
         "level": corpus[2].level, "price_cents": corpus[2].price_cents, "tags": corpus[2].tags},
    ]

    # An id that is not in the candidate set is rejected.
    bad = {"headline": "h", "narrative": "n", "items": [
        {"product_id": corpus[0].product_id, "reason": "a"},
        {"product_id": corpus[1].product_id, "reason": "b"},
        {"product_id": "not-a-real-id", "reason": "c"},
    ]}
    errors, grounded = _validate(bad, candidates, active_ids)
    assert errors and len(grounded) == 2  # the ungrounded one is not counted, and errors are recorded

    # A duplicate id is rejected.
    dup = {"headline": "h", "narrative": "n", "items": [
        {"product_id": corpus[0].product_id, "reason": "a"},
        {"product_id": corpus[0].product_id, "reason": "a2"},
        {"product_id": corpus[1].product_id, "reason": "b"},
    ]}
    errors, _ = _validate(dup, candidates, active_ids)
    assert any("duplicate" in e for e in errors)

    # Fewer than three grounded items is rejected.
    under = {"headline": "h", "narrative": "n", "items": [
        {"product_id": corpus[0].product_id, "reason": "a"},
    ]}
    errors, _ = _validate(under, candidates, active_ids)
    assert any("grounded items" in e for e in errors)


def test_validate_gate_accepts_clean_grounded_draft() -> None:
    corpus = _corpus()
    active_ids = {doc.product_id for doc in corpus}
    candidates = [
        {"product_id": doc.product_id, "title": doc.title, "category": doc.category,
         "level": doc.level, "price_cents": doc.price_cents, "tags": doc.tags}
        for doc in corpus[:4]
    ]
    clean = {"headline": "h", "narrative": "grounded in your behavior", "items": [
        {"product_id": corpus[0].product_id, "reason": "a"},
        {"product_id": corpus[1].product_id, "reason": "b"},
        {"product_id": corpus[2].product_id, "reason": "c"},
    ]}
    errors, grounded = _validate(clean, candidates, active_ids)
    assert errors == []
    assert [g["product_id"] for g in grounded] == [c["product_id"] for c in candidates[:3]]


def test_validate_gate_flags_price_claim_absent_from_metadata() -> None:
    corpus = _corpus()
    active_ids = {doc.product_id for doc in corpus}
    candidates = [
        {"product_id": doc.product_id, "title": doc.title, "category": doc.category,
         "level": doc.level, "price_cents": doc.price_cents, "tags": doc.tags}
        for doc in corpus[:3]
    ]
    # $999 is not any candidate's price → the narrative claim must be flagged.
    draft = {"headline": "h", "narrative": "Get all three for just $999 today!", "items": [
        {"product_id": corpus[0].product_id, "reason": "a"},
        {"product_id": corpus[1].product_id, "reason": "b"},
        {"product_id": corpus[2].product_id, "reason": "c"},
    ]}
    errors, _ = _validate(draft, candidates, active_ids)
    assert any("price" in e for e in errors)


# --------------------------------------------------------------------------------------------------
# Tests — hybrid retrieval respects the seen-exclusion and top-k
# --------------------------------------------------------------------------------------------------
def test_hybrid_retrieve_excludes_seen_and_caps_topk() -> None:
    corpus = _corpus()
    doc_vectors = {doc.product_id: _vec(doc.title) for doc in corpus}
    seen = {corpus[0].product_id}
    candidates = hybrid_retrieve(
        queries=["rag vector search"],
        query_vectors=[_vec("rag vector search")],
        centroid=_vec(corpus[0].title),
        corpus=corpus,
        doc_vectors=doc_vectors,
        filters=RetrievalFilters(),
        exclude_ids=seen,
        extra_ranked_lists=[[d.product_id for d in corpus]],
    )
    ids = [c.product_id for c in candidates]
    assert corpus[0].product_id not in ids          # already-seen course is excluded
    assert len(ids) <= 12                            # top-k cap
    assert len(ids) == len(set(ids))                 # no duplicates
    assert all(pid in {d.product_id for d in corpus} for pid in ids)
