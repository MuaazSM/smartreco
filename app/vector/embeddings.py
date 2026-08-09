"""Product embedding helper on top of the single Mesh gateway (PRD §6.2, §6.5; CLAUDE.md #1/#7).

This module never constructs an AI client and never imports an embedding library — it is a thin,
product-shaped layer over ``app.llm.mesh.embed`` (the ONLY embedding path in the codebase). Its two
jobs:

  * derive the canonical *embeddable text* and its ``content_hash`` for a product row, so the catalog
    service can store ``products.content_hash`` and the outbox worker can key its cache by it;
  * embed that text, skipping the Mesh call entirely when the same ``content_hash`` has already been
    embedded in this process (a ``content_hash`` → vector cache, PRD §6.2 "skip re-embed when
    unchanged"). This sits *in front of* ``mesh.embed``'s own text cache: a content-hash hit here
    means ``mesh.embed`` is never even called, which is exactly what the dual-write invariant test
    asserts.

The hash covers only the *embeddable* fields (title, description, category, level, tags). Metadata
that lives in the Qdrant payload but not the vector — ``price_cents``, ``is_active`` — deliberately
does not move the hash, so a price change re-syncs the point's payload without re-embedding.
"""

from __future__ import annotations

from collections.abc import Sequence

from app.core.logging import get_logger
from app.llm import mesh  # module-qualified so tests can monkeypatch mesh.embed

logger = get_logger(__name__)

EMBEDDING_DIM = mesh.EMBEDDING_DIM

# content_hash -> embedding vector. Lets the outbox worker skip re-embedding text it has already
# embedded this process (PRD §6.2). Distinct from mesh.py's per-text cache: a hit here short-circuits
# before mesh.embed is called at all.
_vector_cache: dict[str, list[float]] = {}


def embeddable_text(
    *,
    title: str,
    description: str,
    category: str,
    tags: Sequence[str],
    level: str,
) -> str:
    """Build the canonical text embedded for a product. Stable field order → stable content_hash.

    Only fields that should influence semantic similarity go in here; ``price_cents``/``is_active``
    are Qdrant-payload metadata, not embedded content, so they are intentionally excluded.
    """
    tag_str = ", ".join(tags)
    return (
        f"{title}\n\n{description}\n\n"
        f"Category: {category}\nLevel: {level}\nTags: {tag_str}"
    )


def content_hash_for(text: str) -> str:
    """Content hash of the embeddable text — the value stored in ``products.content_hash``.

    Delegates to ``mesh.content_hash`` so there is exactly one hashing definition in the codebase.
    """
    return mesh.content_hash(text)


async def embed_product(
    text: str,
    *,
    content_hash: str | None = None,
    use_cache: bool = True,
) -> list[float]:
    """Embed one product's text via Mesh, skipping the call on a ``content_hash`` cache hit.

    Returns a single 1536-dim vector. When ``content_hash`` is supplied and already cached, this
    returns immediately without touching ``mesh.embed`` — the "skip re-embed when unchanged" path.
    """
    digest = content_hash or content_hash_for(text)
    if use_cache:
        cached = _vector_cache.get(digest)
        if cached is not None:
            return cached

    result = await mesh.embed([text], use_cache=True)
    vector = result.vectors[0]
    if len(vector) != EMBEDDING_DIM:  # defensive: a mismatched dim would corrupt the collection
        raise ValueError(
            f"embedding dim {len(vector)} != expected {EMBEDDING_DIM} (product embed_text)"
        )
    if use_cache:
        _vector_cache[digest] = vector
    return vector


def clear_vector_cache() -> None:
    """Drop the in-process content_hash → vector cache (tests / long-running workers)."""
    _vector_cache.clear()
