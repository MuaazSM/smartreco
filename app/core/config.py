"""Central application configuration.

Every environment variable the app reads lives here as a typed `Settings` field — nothing in the
codebase should read `os.environ` directly (see CLAUDE.md, IMPLEMENTATION.md Phase 0 cross-cutting
concerns table). Values come from the process environment first, then `.env` (gitignored; copy
`.env.example` to get started).

Defaults are the docker-compose local topology so `docker compose up -d` + `uvicorn app.main:app`
works with zero configuration. Every var listed in PRD §13.4 is overridden via real platform
environment variables at deploy time — never baked into the image.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Annotated

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

# Optional secret/credential fields where `.env.example`'s blank placeholder (KEY=) must mean
# "unset", not the literal empty string — an empty string is truthy-adjacent enough to trip up
# libraries that branch on `api_key is not None` (e.g. qdrant-client's insecure-connection warning).
_BLANK_MEANS_UNSET_FIELDS = (
    "qdrant_api_key",
    "mesh_api_key",
    "mesh_cheap_model",
    "mesh_quality_model",
    "smtp_host",
    "smtp_user",
    "smtp_password",
    "smtp_from",
    "resend_api_key",
    "langsmith_api_key",
    "digest_trigger_token",
)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # --- app ---
    environment: str = "local"  # local | production
    log_level: str = "INFO"
    app_name: str = "SmartReco"
    port: int = 8000

    # --- database (Postgres; Neon in production, ?sslmode=require) ---
    database_url: str = (
        "postgresql+asyncpg://smartreco:smartreco_local_dev@localhost:5432/smartreco"
    )

    # --- vector store ---
    qdrant_url: str = "http://localhost:6333"
    qdrant_api_key: str | None = None

    # --- cache / locks (Upstash in production) ---
    redis_url: str = "redis://localhost:6379/0"

    # --- mesh api — the ONLY LLM/embedding provider (CLAUDE.md invariant #1) ---
    mesh_api_key: str | None = None
    mesh_base_url: str = "https://api.meshapi.ai/v1"
    mesh_disabled: bool = False  # true only locally, per PRD §13.4
    # Optional per-tier model override. Empty = the free-first router in app.llm.model_router decides
    # (the default; keeps CI/offline behavior). Point these at fast paid models once the Mesh account
    # has a balance so the agent finishes inside its time budget instead of timing out to the
    # deterministic fallback. Still routed through Mesh and still the router's single decision point
    # (invariant #1) — these are Mesh model ids, resolved by model_router.resolve().
    mesh_cheap_model: str | None = None
    mesh_quality_model: str | None = None
    # Agent wall-clock cap (seconds). Default 25.0 keeps the spec/CI budget; raise it locally when the
    # Mesh gateway is slow so the full seven-node graph finishes instead of timing out to the
    # deterministic fallback. Still a hard, bounded cap (asyncio.wait_for in app/agent/graph.py).
    agent_timeout_seconds: float = 25.0

    # --- auth (bodies implemented in Phase 3) ---
    jwt_secret: str = "dev-insecure-secret-change-me"
    jwt_algorithm: str = "HS256"
    jwt_expire_minutes: int = 1440  # 24h
    bcrypt_rounds: int = 12

    # --- cookies / CORS ---
    cookie_samesite: str = "lax"  # lax | none — see PRD §13.2
    # NoDecode: read as a raw comma-separated string from the env, not JSON — see validator below.
    cors_origins: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["http://localhost:3000"]
    )
    frontend_url: str = "http://localhost:3000"

    # --- digest email (bonus F7) ---
    smtp_host: str | None = None
    smtp_port: int = 587
    smtp_user: str | None = None
    smtp_password: str | None = None
    smtp_from: str | None = None
    resend_api_key: str | None = None

    # --- observability (bonus F8) ---
    langsmith_api_key: str | None = None
    langsmith_project: str = "smartreco"
    langsmith_tracing: bool = False

    # --- scheduled digest trigger endpoint (GitHub Actions → protected endpoint, PRD §13.3) ---
    digest_trigger_token: str | None = None

    @field_validator("cors_origins", mode="before")
    @classmethod
    def _split_cors_origins(cls, value: str | list[str]) -> list[str]:
        if isinstance(value, str):
            return [origin.strip() for origin in value.split(",") if origin.strip()]
        return value

    @model_validator(mode="after")
    def _blank_optional_secrets_are_unset(self) -> "Settings":
        for name in _BLANK_MEANS_UNSET_FIELDS:
            if getattr(self, name) == "":
                setattr(self, name, None)
        return self

    @property
    def is_local(self) -> bool:
        return self.environment == "local"


@lru_cache
def get_settings() -> Settings:
    """Process-wide cached settings singleton."""
    return Settings()


settings = get_settings()
