"""Password hashing and JWT helpers.

Scaffolding stub for Phase 0. Function bodies are implemented in Phase 3
(IMPLEMENTATION.md "Phase 3 — Authentication (F1)"): bcrypt cost-12 password hashing and HS256
JWTs carrying a `role` claim with a 24h expiry, per CLAUDE.md conventions. Signatures are fixed
now so Phase 1 (users table) and later phases can be written against a stable contract.
"""

from __future__ import annotations

from typing import Any


def hash_password(plain_password: str) -> str:
    """Hash a plaintext password with bcrypt (cost 12). Implemented in Phase 3."""
    raise NotImplementedError("app.core.security.hash_password is implemented in Phase 3")


def verify_password(plain_password: str, hashed_password: str) -> bool:
    """Verify a plaintext password against its bcrypt hash. Implemented in Phase 3."""
    raise NotImplementedError("app.core.security.verify_password is implemented in Phase 3")


def create_access_token(subject: str, role: str) -> str:
    """Encode an HS256 JWT for `subject` carrying a `role` claim, 24h expiry. Phase 3."""
    raise NotImplementedError("app.core.security.create_access_token is implemented in Phase 3")


def decode_access_token(token: str) -> dict[str, Any]:
    """Decode and validate an HS256 JWT, returning its claims. Implemented in Phase 3."""
    raise NotImplementedError("app.core.security.decode_access_token is implemented in Phase 3")
