"""Profile-builder unit tests (PRD §6.3; IMPLEMENTATION.md Phase 5).

These exercise the profile math directly and deterministically — no DB, no Qdrant, no network — so
they pin the invariants that matter: the **decayed interest centroid** (3-day half-life weighted
average of product vectors), the decayed category/term/level maps, the drift (cosine-distance)
accessor Phase 7 consumes, and the stability of ``profile_hash`` (the L1 cache key). The end-to-end
DB+Qdrant path is covered by ``tests/test_events.py``.

No LLM/embedding is ever touched here (the profile builder is LLM-free by design).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import pytest

from app.services import profile
from app.services.event_ingest import weight_for


@dataclass
class _StubEvent:
    """Minimal stand-in for a persisted ``Event`` row — only the fields ``_compute`` reads."""

    event_type: str
    weight: float
    client_ts: datetime
    product_id: uuid.UUID | None = None
    payload: dict | None = None


def _now() -> datetime:
    return datetime(2026, 8, 9, 12, 0, 0, tzinfo=timezone.utc)


def test_decay_factor_half_life_is_three_days() -> None:
    now = _now()
    assert profile._decay_factor(now, now) == pytest.approx(1.0)
    assert profile._decay_factor(now - timedelta(days=3), now) == pytest.approx(0.5, rel=1e-6)
    assert profile._decay_factor(now - timedelta(days=6), now) == pytest.approx(0.25, rel=1e-6)
    # A future client_ts (clock skew) never boosts weight above 1.0.
    assert profile._decay_factor(now + timedelta(days=5), now) == pytest.approx(1.0)


def test_decayed_centroid_is_a_recency_weighted_average() -> None:
    now = _now()
    prod_a, prod_b = uuid.uuid4(), uuid.uuid4()
    vectors = {str(prod_a): [1.0, 0.0], str(prod_b): [0.0, 1.0]}

    events = [
        # A: weight 1.0, age 0 -> decay 1.0 -> effective 1.0
        (_StubEvent("view", 1.0, now, prod_a, {}), "Data Science", "beginner"),
        # B: weight 1.0, age 3 days -> decay 0.5 -> effective 0.5
        (_StubEvent("view", 1.0, now - timedelta(days=3), prod_b, {}), "Web Development", "advanced"),
    ]

    interest_vector, top_categories, _terms, level_affinity = profile._compute(events, vectors, now)

    # centroid = (1.0*[1,0] + 0.5*[0,1]) / 1.5 = [0.6667, 0.3333]
    assert interest_vector[0] == pytest.approx(2 / 3, rel=1e-4)
    assert interest_vector[1] == pytest.approx(1 / 3, rel=1e-4)
    # Categories/levels carry the same decayed weights, strongest first.
    assert top_categories == {"Data Science": 1.0, "Web Development": 0.5}
    assert list(top_categories)[0] == "Data Science"
    assert level_affinity == {"beginner": 1.0, "advanced": 0.5}


def test_search_events_feed_top_terms_not_the_centroid() -> None:
    now = _now()
    events = [
        (_StubEvent("search", 2.0, now, None, {"query": "Deep Learning with PyTorch"}), None, None),
        (_StubEvent("search", 2.0, now, None, {"query": "pytorch tips"}), None, None),
    ]
    interest_vector, top_categories, top_terms, _levels = profile._compute(events, {}, now)

    assert interest_vector == []  # no product vectors -> empty centroid
    assert top_categories == {}
    # "with" is a stopword and dropped; "pytorch" appears in both searches -> highest weight.
    assert "with" not in top_terms
    assert top_terms["pytorch"] == pytest.approx(4.0)
    assert list(top_terms)[0] == "pytorch"


def test_cosine_distance_accessor() -> None:
    assert profile.cosine_distance([1.0, 0.0], [1.0, 0.0]) == pytest.approx(0.0)
    assert profile.cosine_distance([1.0, 0.0], [0.0, 1.0]) == pytest.approx(1.0)
    assert profile.cosine_distance([1.0, 0.0], [-1.0, 0.0]) == pytest.approx(2.0)
    # Missing / empty / mismatched-shape inputs read as "no measurable drift", never an error.
    assert profile.cosine_distance([], [1.0, 0.0]) == 0.0
    assert profile.cosine_distance(None, None) == 0.0
    assert profile.cosine_distance([1.0, 0.0, 0.0], [1.0, 0.0]) == 0.0


def test_profile_hash_is_stable_and_input_sensitive() -> None:
    vec = [0.1, 0.2, 0.3]
    cats = {"AI & LLMs": 3.0, "Machine Learning": 1.0}
    terms = {"rag": 2.0}
    levels = {"advanced": 3.0}

    h1 = profile._profile_hash(vec, cats, terms, levels)
    h2 = profile._profile_hash(list(vec), dict(cats), dict(terms), dict(levels))
    assert h1 == h2  # identical inputs -> identical L1 key

    # A moved centroid changes the key...
    assert profile._profile_hash([0.1, 0.2, 0.4], cats, terms, levels) != h1
    # ...as does a change in categories or terms.
    assert profile._profile_hash(vec, {"AI & LLMs": 4.0}, terms, levels) != h1
    assert profile._profile_hash(vec, cats, {"rag": 2.0, "agents": 1.0}, levels) != h1
    # Float jitter below the quantization threshold does NOT change the key (cache stays warm).
    assert profile._profile_hash([0.1, 0.2, 0.300000001], cats, terms, levels) == h1


def test_server_weighting_matches_prd_table() -> None:
    assert weight_for("view", None) == 1.0
    assert weight_for("click", None) == 1.5
    assert weight_for("search", {"query": "x"}) == 2.0
    assert weight_for("cart", None) == 3.0
    assert weight_for("dwell", {"dwell_ms": 45_000}) == 2.5  # >30s -> strong
    assert weight_for("dwell", {"dwell_ms": 5_000}) == 1.0   # brief glance -> view-level
    assert weight_for("dwell", {"dwell_seconds": 40}) == 2.5  # seconds accepted too
