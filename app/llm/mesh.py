"""The single Mesh AI gateway — the ONLY LLM/embedding client in the codebase (CLAUDE.md #1, #7).

Nothing else in the repository may construct an LLM client; `tests/test_single_gateway.py` enforces
that this file holds the one and only ``AsyncOpenAI(...)`` construction and that no banned provider
SDK is imported anywhere. A submission that routes any AI call outside Mesh is disqualified (PRD §7).

Responsibilities (PRD §7, §6.5, §6.5.1):
  * per-node model selection delegated to ``model_router.resolve`` — never a hardcoded id at a call site
  * ``tenacity`` exponential backoff on 429/5xx, 20-second timeout
  * token + cost accounting returned as ``MeshUsage`` and pushed to an optional ``on_usage`` recorder
    (Phase 6/7 wires that recorder to persist into ``agent_runs`` — this module never touches the DB)
  * Pydantic-validated JSON mode for structured node outputs
  * embeddings via ``/v1/embeddings`` (``openai/text-embedding-3-small``, 1536-dim) with a content-hash
    cache so unchanged text is never re-embedded
  * ``MESH_DISABLED=true`` short-circuit returning recorded fixtures for every node and deterministic
    pseudo-vectors for embeddings, so the whole app builds and tests offline with zero AI network calls
"""

from __future__ import annotations

import hashlib
import inspect
import json
import logging
import math
import random
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Generic, Sequence, TypeVar

from openai import (
    APIConnectionError,
    APIStatusError,
    APITimeoutError,
    AsyncOpenAI,
    InternalServerError,
    RateLimitError,
)
from pydantic import BaseModel, ValidationError
from tenacity import (
    before_sleep_log,
    retry,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
)

from app.core.config import settings
from app.core.logging import get_logger
from app.llm import model_router

logger = get_logger(__name__)

T = TypeVar("T")
TModel = TypeVar("TModel", bound=BaseModel)

# A chat message as the OpenAI SDK expects it: ``{"role": ..., "content": ...}``.
ChatMessages = list[dict[str, str]]

# Callback the agent layer supplies to persist telemetry into ``agent_runs``. May be sync or async;
# we await it if it returns an awaitable. This is the decoupling seam — mesh.py never imports app.db.
UsageRecorder = Callable[["MeshUsage"], Awaitable[None] | None]

EMBEDDING_DIM = 1536
_TIMEOUT_SECONDS = 20.0
_FIXTURES_DIR = Path(__file__).parent / "fixtures"

# Best-effort list prices, USD per 1M tokens (prompt, completion), for cost accounting in agent_runs.
# Free-tier and unknown models are billed at $0 — this is telemetry, not invoicing.
_PRICING: dict[str, tuple[float, float]] = {
    "openai/gpt-4o-mini": (0.15, 0.60),
    "anthropic/claude-sonnet-4.5": (3.00, 15.00),
    "openai/text-embedding-3-small": (0.02, 0.0),
}

# In-process content-hash embedding cache (PRD §6.5.1 L3). Product-level dedupe also happens in
# Postgres via products.content_hash (Phase 4); this avoids re-embedding identical text within a run.
_embedding_cache: dict[str, list[float]] = {}

# Lazily constructed process-wide Mesh client (see get_client). Placeholder key keeps construction
# offline-safe; real calls guard on a present key first.
_client: AsyncOpenAI | None = None
_PLACEHOLDER_KEY = "MESH_DISABLED_NO_NETWORK"


# --------------------------------------------------------------------------------------------------
# Errors
# --------------------------------------------------------------------------------------------------
class MeshError(RuntimeError):
    """Base class for all Mesh gateway failures."""


class MeshConfigError(MeshError):
    """MESH_API_KEY missing while Mesh is enabled — cannot make a live call."""


class MeshStructuredOutputError(MeshError):
    """The model returned content that failed Pydantic validation against the response model."""

    def __init__(self, message: str, raw_text: str) -> None:
        super().__init__(message)
        self.raw_text = raw_text


class MeshFixtureError(MeshError):
    """A MESH_DISABLED fixture is missing or does not match the caller's response model."""


# --------------------------------------------------------------------------------------------------
# Result / usage records (returned to callers; agent layer persists them to agent_runs)
# --------------------------------------------------------------------------------------------------
@dataclass(slots=True)
class MeshUsage:
    """Token + cost accounting for one Mesh call. Handed to ``on_usage`` for agent_runs (Phase 6/7)."""

    node: str
    model: str
    tier: str
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    cost_usd: float
    latency_ms: float
    cache_hit: bool = False
    disabled: bool = False

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class MeshResult(Generic[T]):
    """A completion plus its parsed payload and usage. ``data`` is a Pydantic model (structured) or str."""

    data: T
    text: str
    model: str
    usage: MeshUsage


@dataclass(slots=True)
class MeshEmbeddingResult:
    """Embedding vectors (input order) plus usage and how many came from the content-hash cache."""

    vectors: list[list[float]]
    usage: MeshUsage
    cache_hits: int = 0
    dim: int = field(default=EMBEDDING_DIM)


# --------------------------------------------------------------------------------------------------
# Client construction — THE ONLY AsyncOpenAI in the codebase (CLAUDE.md invariant #1)
# --------------------------------------------------------------------------------------------------
def get_client() -> AsyncOpenAI:
    """Return the process-wide Mesh client, constructing it on first use.

    This holds the single ``AsyncOpenAI(...)`` construction in the whole repository (PRD §7). It is
    never reached under ``MESH_DISABLED=true`` — every public entry point short-circuits to fixtures
    first — so the placeholder api_key can never leak into a network call.
    """
    global _client
    if _client is None:
        _client = AsyncOpenAI(
            base_url=settings.mesh_base_url,
            api_key=settings.mesh_api_key or _PLACEHOLDER_KEY,
            timeout=_TIMEOUT_SECONDS,
            max_retries=0,  # retries are owned by tenacity below, not the SDK
        )
    return _client


# --------------------------------------------------------------------------------------------------
# Retry policy — exponential backoff on 429 and 5xx only (PRD §7)
# --------------------------------------------------------------------------------------------------
def _is_retryable(exc: BaseException) -> bool:
    if isinstance(exc, (RateLimitError, APITimeoutError, APIConnectionError, InternalServerError)):
        return True
    if isinstance(exc, APIStatusError):
        return exc.status_code == 429 or exc.status_code >= 500
    return False


_RETRY_KWARGS: dict[str, Any] = dict(
    reraise=True,
    retry=retry_if_exception(_is_retryable),
    wait=wait_exponential(multiplier=0.5, min=0.5, max=8.0),
    stop=stop_after_attempt(4),
    before_sleep=before_sleep_log(logger, logging.WARNING),
)


@retry(**_RETRY_KWARGS)
async def _chat_completion(client: AsyncOpenAI, **kwargs: Any) -> Any:
    return await client.chat.completions.create(**kwargs)


@retry(**_RETRY_KWARGS)
async def _embeddings_create(client: AsyncOpenAI, **kwargs: Any) -> Any:
    return await client.embeddings.create(**kwargs)


# --------------------------------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------------------------------
def _estimate_cost(model: str, prompt_tokens: int, completion_tokens: int) -> float:
    price_in, price_out = _PRICING.get(model, (0.0, 0.0))
    return (prompt_tokens * price_in + completion_tokens * price_out) / 1_000_000


def content_hash(text: str) -> str:
    """Stable content hash used as the embedding cache key (mirrors products.content_hash intent)."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _pseudo_embedding(text: str, dim: int = EMBEDDING_DIM) -> list[float]:
    """Deterministic unit-norm pseudo-vector seeded from the text, for offline (MESH_DISABLED) use.

    Stable across runs (seeded from a SHA-256 of the text) so downstream cosine/MMR math is fully
    reproducible offline. This is a development fixture only — never a runtime non-Mesh path.
    """
    seed = int.from_bytes(hashlib.sha256(text.encode("utf-8")).digest()[:8], "big")
    rng = random.Random(seed)
    vector = [rng.gauss(0.0, 1.0) for _ in range(dim)]
    norm = math.sqrt(sum(component * component for component in vector)) or 1.0
    return [component / norm for component in vector]


async def _emit_usage(on_usage: UsageRecorder | None, usage: MeshUsage) -> None:
    if on_usage is None:
        return
    result = on_usage(usage)
    if inspect.isawaitable(result):
        await result


def _load_fixture(node: str) -> dict[str, Any]:
    path = _FIXTURES_DIR / f"{node}.json"
    if not path.exists():
        raise MeshFixtureError(
            f"no MESH_DISABLED fixture for node {node!r} at {path}. Add one (see app/llm/fixtures/)."
        )
    return json.loads(path.read_text(encoding="utf-8"))


def _require_live_client() -> AsyncOpenAI:
    if not settings.mesh_api_key:
        raise MeshConfigError(
            "MESH_API_KEY is not set while MESH_DISABLED is false. Set the key, or run with "
            "MESH_DISABLED=true to use offline fixtures."
        )
    return get_client()


# --------------------------------------------------------------------------------------------------
# Public API — structured completion (nodes: plan_queries, grade_retrieval, generate, rerank)
# --------------------------------------------------------------------------------------------------
async def complete_structured(
    node: str,
    messages: ChatMessages,
    response_model: type[TModel],
    *,
    tier: str | None = None,
    temperature: float = 0.2,
    max_tokens: int | None = None,
    on_usage: UsageRecorder | None = None,
) -> MeshResult[TModel]:
    """Run a JSON-mode chat completion and validate it into ``response_model`` (PRD §7 structured output).

    Model choice flows through ``model_router.resolve(node, tier)`` — never hardcoded here. The caller
    (Phase 6) supplies the Pydantic ``response_model`` and prompts that instruct the model to emit JSON.
    Under ``MESH_DISABLED=true`` the node's recorded fixture is validated into ``response_model`` and
    returned with zero network I/O.
    """
    model = model_router.resolve(node, tier)
    resolved_tier = tier or model_router.NODE_TIERS.get(node, "cheap")
    start = time.perf_counter()

    if settings.mesh_disabled:
        payload = _load_fixture(node)
        text = json.dumps(payload)
        try:
            data = response_model.model_validate(payload)
        except ValidationError as exc:
            raise MeshFixtureError(
                f"MESH_DISABLED fixture for node {node!r} does not match {response_model.__name__}: "
                f"{exc}. Align app/llm/fixtures/{node}.json with the Phase 6 schema."
            ) from exc
        usage = MeshUsage(
            node=node,
            model=model,
            tier=resolved_tier,
            prompt_tokens=0,
            completion_tokens=0,
            total_tokens=0,
            cost_usd=0.0,
            latency_ms=(time.perf_counter() - start) * 1000,
            disabled=True,
        )
        await _emit_usage(on_usage, usage)
        return MeshResult(data=data, text=text, model=model, usage=usage)

    client = _require_live_client()
    request: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "response_format": {"type": "json_object"},
    }
    if max_tokens is not None:
        request["max_tokens"] = max_tokens

    completion = await _chat_completion(client, **request)
    text = completion.choices[0].message.content or ""
    try:
        data = response_model.model_validate_json(text)
    except ValidationError as exc:
        raise MeshStructuredOutputError(
            f"node {node!r} returned JSON that failed {response_model.__name__} validation: {exc}",
            raw_text=text,
        ) from exc

    prompt_tokens = getattr(completion.usage, "prompt_tokens", 0) or 0
    completion_tokens = getattr(completion.usage, "completion_tokens", 0) or 0
    usage = MeshUsage(
        node=node,
        model=model,
        tier=resolved_tier,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        total_tokens=prompt_tokens + completion_tokens,
        cost_usd=_estimate_cost(model, prompt_tokens, completion_tokens),
        latency_ms=(time.perf_counter() - start) * 1000,
    )
    await _emit_usage(on_usage, usage)
    return MeshResult(data=data, text=text, model=model, usage=usage)


# --------------------------------------------------------------------------------------------------
# Public API — plain text completion (fallback narratives, ad-hoc prose)
# --------------------------------------------------------------------------------------------------
async def complete_text(
    node: str,
    messages: ChatMessages,
    *,
    tier: str | None = None,
    temperature: float = 0.7,
    max_tokens: int | None = None,
    on_usage: UsageRecorder | None = None,
) -> MeshResult[str]:
    """Run a plain chat completion returning raw text. Model choice via ``model_router.resolve``.

    Under ``MESH_DISABLED=true`` returns the node fixture's ``narrative`` field if present, else its
    JSON dump — enough for offline rendering without a network call.
    """
    model = model_router.resolve(node, tier)
    resolved_tier = tier or model_router.NODE_TIERS.get(node, "cheap")
    start = time.perf_counter()

    if settings.mesh_disabled:
        payload = _load_fixture(node)
        text = payload.get("narrative") if isinstance(payload, dict) else None
        text = text if isinstance(text, str) else json.dumps(payload)
        usage = MeshUsage(
            node=node,
            model=model,
            tier=resolved_tier,
            prompt_tokens=0,
            completion_tokens=0,
            total_tokens=0,
            cost_usd=0.0,
            latency_ms=(time.perf_counter() - start) * 1000,
            disabled=True,
        )
        await _emit_usage(on_usage, usage)
        return MeshResult(data=text, text=text, model=model, usage=usage)

    client = _require_live_client()
    request: dict[str, Any] = {"model": model, "messages": messages, "temperature": temperature}
    if max_tokens is not None:
        request["max_tokens"] = max_tokens

    completion = await _chat_completion(client, **request)
    text = completion.choices[0].message.content or ""
    prompt_tokens = getattr(completion.usage, "prompt_tokens", 0) or 0
    completion_tokens = getattr(completion.usage, "completion_tokens", 0) or 0
    usage = MeshUsage(
        node=node,
        model=model,
        tier=resolved_tier,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        total_tokens=prompt_tokens + completion_tokens,
        cost_usd=_estimate_cost(model, prompt_tokens, completion_tokens),
        latency_ms=(time.perf_counter() - start) * 1000,
    )
    await _emit_usage(on_usage, usage)
    return MeshResult(data=text, text=text, model=model, usage=usage)


# --------------------------------------------------------------------------------------------------
# Public API — embeddings (Phase 4 vector layer; the ONLY embedding path, CLAUDE.md invariant #7)
# --------------------------------------------------------------------------------------------------
async def embed(
    texts: Sequence[str],
    *,
    use_cache: bool = True,
    on_usage: UsageRecorder | None = None,
) -> MeshEmbeddingResult:
    """Embed ``texts`` via Mesh ``/v1/embeddings`` (1536-dim), returning vectors in input order.

    A content-hash cache skips re-embedding identical text (PRD §6.5.1 L3). Under
    ``MESH_DISABLED=true`` returns deterministic unit-norm pseudo-vectors so offline cosine/MMR math
    works and is stable across runs. Model choice flows through ``model_router.resolve('embeddings')``.
    """
    model = model_router.resolve("embeddings")
    start = time.perf_counter()
    vectors: list[list[float] | None] = [None] * len(texts)
    hashes = [content_hash(text) for text in texts]

    cache_hits = 0
    pending: list[int] = []
    for index, digest in enumerate(hashes):
        cached = _embedding_cache.get(digest) if use_cache else None
        if cached is not None:
            vectors[index] = cached
            cache_hits += 1
        else:
            pending.append(index)

    total_tokens = 0
    if pending and settings.mesh_disabled:
        for index in pending:
            vector = _pseudo_embedding(texts[index])
            vectors[index] = vector
            if use_cache:
                _embedding_cache[hashes[index]] = vector
            total_tokens += max(1, len(texts[index]) // 4)
    elif pending:
        client = _require_live_client()
        response = await _embeddings_create(
            client, model=model, input=[texts[index] for index in pending]
        )
        for slot, item in zip(pending, response.data):
            vector = list(item.embedding)
            vectors[slot] = vector
            if use_cache:
                _embedding_cache[hashes[slot]] = vector
        total_tokens = getattr(response.usage, "total_tokens", 0) or 0

    usage = MeshUsage(
        node="embeddings",
        model=model,
        tier="cheap",
        prompt_tokens=total_tokens,
        completion_tokens=0,
        total_tokens=total_tokens,
        cost_usd=_estimate_cost(model, total_tokens, 0),
        latency_ms=(time.perf_counter() - start) * 1000,
        cache_hit=not pending,
        disabled=settings.mesh_disabled,
    )
    await _emit_usage(on_usage, usage)
    # Every slot is filled by construction (cache hit, fixture, or API response).
    return MeshEmbeddingResult(
        vectors=[vector for vector in vectors if vector is not None],
        usage=usage,
        cache_hits=cache_hits,
    )


def clear_embedding_cache() -> None:
    """Drop the in-process embedding cache (used by tests and long-running workers)."""
    _embedding_cache.clear()
