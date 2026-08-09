"""Reranker invariant tests (PRD §6.4 retrieval polish; §9d bonus polish). Keep strong.

``app/vector/reranker.py`` is the optional pairwise reranker the ``retrieve`` node calls when
``ctx.enable_rerank`` is set. It must never be able to weaken the grounding gate, so what matters here
is not "does it produce a better order" (unknowable offline — see below) but:

  * ``rerank(..., use_llm=True)`` always returns a **permutation** of the input candidates — same ids,
    same count, nothing invented, nothing dropped;
  * it degrades to the identity (pass-through) order on any Mesh failure;
  * it is deterministic (stable sort — ties keep their original relative order);
  * the offline ``MESH_DISABLED`` fixture path (``app/llm/fixtures/rerank.json``) round-trips end to
    end without error, remapping the fixture's ``PLACEHOLDER_PRODUCT_N`` sentinel ids onto real
    candidates instead of silently scoring nothing;
  * the measurement harness (``measure_rerank_lift``) correctly reports a structural before/after
    delta for a controlled reordering.

Honesty note (mirrors the module docstring): none of these tests claim a semantic quality "lift". Under
``MESH_DISABLED=true`` the vectors feeding retrieval are seeded pseudo-random (``app/llm/mesh.py``
``_pseudo_embedding``), so there is no meaningful notion of "better" here — only "did the mechanism
reorder things safely and measurably". A real lift number needs a live ``MESH_API_KEY`` and live
embeddings; nothing in this file fabricates one.

No LLM/embedding client is constructed here — the only model access is the Mesh gateway's offline
fixtures / a monkeypatched stub (invariants #1/#7).
"""

from __future__ import annotations

import os

# Offline: rerank's Mesh call resolves to the recorded fixture (or a monkeypatched stub) — zero
# network I/O, matching every other invariant test in this suite.
os.environ.setdefault("MESH_DISABLED", "true")
os.environ.setdefault("SMARTRECO_RUN_SCHEDULER", "false")

import pytest  # noqa: E402

from app.agent.schemas import RerankEntry, RerankOutput  # noqa: E402
from app.llm import mesh  # noqa: E402
from app.llm.mesh import MeshResult, MeshUsage  # noqa: E402
from app.vector import reranker  # noqa: E402
from app.vector.hybrid_retriever import Candidate  # noqa: E402

# pytest.ini sets asyncio_mode = auto — every `async def test_*` below runs as an asyncio test with no
# explicit marker needed (matches the rest of this suite, e.g. tests/test_grounding.py).


# --------------------------------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------------------------------
def _candidates(ids: list[str]) -> list[Candidate]:
    return [
        Candidate(
            product_id=pid,
            title=f"Course {pid}",
            category="AI & LLMs",
            level="intermediate",
            price_cents=9900,
            tags=["rag"],
            score=1.0 - index * 0.01,  # arbitrary, descending, distinguishable RRF-style scores
        )
        for index, pid in enumerate(ids)
    ]


def _fake_usage(node: str = "rerank") -> MeshUsage:
    return MeshUsage(
        node=node, model="fake", tier="cheap", prompt_tokens=0, completion_tokens=0,
        total_tokens=0, cost_usd=0.0, latency_ms=0.0, disabled=True,
    )


def _stub_ranking(monkeypatch: pytest.MonkeyPatch, entries: list[RerankEntry]) -> None:
    """Force ``mesh.complete_structured`` to return a controlled ``RerankOutput`` for node 'rerank'."""

    async def fake_complete_structured(node, messages, response_model, **kwargs):
        assert node == "rerank"
        assert response_model is RerankOutput
        return MeshResult(
            data=RerankOutput(ranking=entries), text="", model="fake", usage=_fake_usage()
        )

    monkeypatch.setattr(mesh, "complete_structured", fake_complete_structured)


def _stub_failure(monkeypatch: pytest.MonkeyPatch, exc: Exception) -> None:
    async def fake_complete_structured(node, messages, response_model, **kwargs):
        raise exc

    monkeypatch.setattr(mesh, "complete_structured", fake_complete_structured)


# --------------------------------------------------------------------------------------------------
# Pass-through defaults
# --------------------------------------------------------------------------------------------------
async def test_use_llm_false_is_identity_pass_through() -> None:
    """The documented default: no Mesh call at all, input order preserved exactly."""
    candidates = _candidates(["p1", "p2", "p3"])
    result = await reranker.rerank(["rag basics"], candidates, use_llm=False)
    assert result == candidates


async def test_empty_candidates_short_circuits() -> None:
    result = await reranker.rerank(["rag basics"], [], use_llm=True)
    assert result == []


# --------------------------------------------------------------------------------------------------
# Permutation invariant — the property that matters for grounding safety
# --------------------------------------------------------------------------------------------------
async def test_rerank_is_a_permutation_never_invents_or_drops_ids(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A model response scoring only a subset, plus one id that is not a candidate at all (a
    simulated hallucination), must still yield exactly the original candidate id set — nothing
    invented, nothing dropped."""
    candidates = _candidates(["p1", "p2", "p3", "p4"])
    _stub_ranking(
        monkeypatch,
        [
            RerankEntry(product_id="p3", score=0.9),
            RerankEntry(product_id="p1", score=0.5),
            RerankEntry(product_id="NOT_A_REAL_CANDIDATE", score=0.99),  # hallucinated id — ignored
        ],
    )
    result = await reranker.rerank(["rag"], candidates, use_llm=True)

    assert {c.product_id for c in result} == {"p1", "p2", "p3", "p4"}
    assert len(result) == len(candidates)
    # Scored-highest-first, then unscored candidates keep their original relative order appended.
    assert [c.product_id for c in result] == ["p3", "p1", "p2", "p4"]


async def test_rerank_is_deterministic_stable_sort(monkeypatch: pytest.MonkeyPatch) -> None:
    """Equal scores (or no score at all) must not shuffle candidates — ties keep input order."""
    candidates = _candidates(["p1", "p2", "p3"])
    _stub_ranking(
        monkeypatch,
        [
            RerankEntry(product_id="p1", score=0.5),
            RerankEntry(product_id="p2", score=0.5),
            RerankEntry(product_id="p3", score=0.5),
        ],
    )
    first = await reranker.rerank(["rag"], candidates, use_llm=True)
    second = await reranker.rerank(["rag"], candidates, use_llm=True)
    assert [c.product_id for c in first] == ["p1", "p2", "p3"]
    assert first == second


# --------------------------------------------------------------------------------------------------
# Graceful degradation on Mesh failure
# --------------------------------------------------------------------------------------------------
async def test_rerank_degrades_to_pass_through_on_mesh_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidates = _candidates(["p1", "p2", "p3"])
    _stub_failure(monkeypatch, RuntimeError("simulated Mesh 500"))

    result = await reranker.rerank(["rag"], candidates, use_llm=True)
    assert result == candidates  # identity order — reranking is polish, never a hard dependency


async def test_rerank_degrades_on_malformed_structured_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A Pydantic validation failure inside ``complete_structured`` is also just a Mesh failure from
    the reranker's point of view — same graceful pass-through."""
    from app.llm.mesh import MeshStructuredOutputError

    _stub_failure(monkeypatch, MeshStructuredOutputError("bad json", raw_text="not json"))
    candidates = _candidates(["p1", "p2"])
    result = await reranker.rerank(["rag"], candidates, use_llm=True)
    assert result == candidates


# --------------------------------------------------------------------------------------------------
# Offline fixture path (MESH_DISABLED=true, app/llm/fixtures/rerank.json) — the real wiring
# --------------------------------------------------------------------------------------------------
async def test_offline_fixture_path_round_trips_as_a_valid_permutation() -> None:
    """End to end through the real ``MESH_DISABLED`` fixture (no monkeypatch): the fixture's
    ``PLACEHOLDER_PRODUCT_N`` sentinel ids are remapped onto the Nth real candidate rather than
    silently matching nothing, and the result is still a strict permutation of the input. This is the
    same offline-convenience shim ``app.agent.context.remap_placeholders`` uses for ``generate`` —
    applied here so the offline path exercises real scoring/sorting logic instead of a no-op.
    """
    candidates = _candidates(["p1", "p2", "p3", "p4", "p5"])
    result = await reranker.rerank(["rag pipeline", "vector db"], candidates, use_llm=True)

    assert {c.product_id for c in result} == {c.product_id for c in candidates}
    assert len(result) == len(candidates)
    # Deterministic: running it again yields the exact same order.
    again = await reranker.rerank(["rag pipeline", "vector db"], candidates, use_llm=True)
    assert [c.product_id for c in result] == [c.product_id for c in again]


# --------------------------------------------------------------------------------------------------
# Measured lift — mechanism + harness, explicitly not a semantic quality claim
# --------------------------------------------------------------------------------------------------
async def test_measure_rerank_lift_reports_full_reversal_correctly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A controlled, fully-reversed rerank response is a clean way to prove the harness's math without
    depending on the (coincidentally order-preserving) static offline fixture: Kendall's tau for a
    complete reversal of n>=2 items is exactly -1.0, and every position moves.
    """
    candidates = _candidates(["p1", "p2", "p3", "p4"])
    baseline = await reranker.rerank(["x"], candidates, use_llm=False)  # identity

    _stub_ranking(
        monkeypatch,
        [
            RerankEntry(product_id="p4", score=1.0),
            RerankEntry(product_id="p3", score=0.8),
            RerankEntry(product_id="p2", score=0.6),
            RerankEntry(product_id="p1", score=0.4),
        ],
    )
    reranked = await reranker.rerank(["x"], candidates, use_llm=True)
    assert [c.product_id for c in reranked] == ["p4", "p3", "p2", "p1"]

    report = reranker.measure_rerank_lift(baseline, reranked)
    assert report.baseline_order == ["p1", "p2", "p3", "p4"]
    assert report.reranked_order == ["p4", "p3", "p2", "p1"]
    assert report.moved == 4
    assert report.kendall_tau == pytest.approx(-1.0)
    assert report.top_k_overlap == pytest.approx(1.0)  # same 4 ids, top-k = whole set here
    # The harness must not claim this is a quality improvement — only a structural delta.
    assert "not" in report.note.lower() or "no" in report.note.lower()


async def test_measure_rerank_lift_reports_no_movement_for_identical_orders() -> None:
    candidates = _candidates(["p1", "p2", "p3"])
    report = reranker.measure_rerank_lift(candidates, candidates)
    assert report.moved == 0
    assert report.kendall_tau == pytest.approx(1.0)
    assert report.top_k_overlap == pytest.approx(1.0)


async def test_measure_rerank_lift_single_candidate_has_no_defined_tau() -> None:
    candidates = _candidates(["p1"])
    report = reranker.measure_rerank_lift(candidates, candidates)
    assert report.kendall_tau is None
    assert report.moved == 0


def test_measure_rerank_lift_rejects_mismatched_candidate_sets() -> None:
    baseline = _candidates(["p1", "p2", "p3"])
    different_set = _candidates(["p1", "p2", "p9"])  # not a permutation of baseline
    with pytest.raises(ValueError):
        reranker.measure_rerank_lift(baseline, different_set)
