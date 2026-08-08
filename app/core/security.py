"""Password hashing and JWT helpers.

Phase 3 implementation (IMPLEMENTATION.md "Phase 3 — Authentication (F1)"): bcrypt cost-12 password
hashing and HS256 JWTs carrying a `role` claim with a 24h expiry (`settings.jwt_expire_minutes`), per
CLAUDE.md conventions and PRD §6.1.

Uses the `bcrypt` library directly rather than `passlib.CryptContext`. The pinned
`passlib==1.7.4` cannot drive the installed `bcrypt==5.0.0` backend — its version probe
(`bcrypt.__about__`, removed upstream) and its bundled 72-byte wrap-bug self-test both raise under
bcrypt's current API, so `CryptContext.hash()` crashes on every call in this environment (verified
directly; not a theoretical concern). `bcrypt` is already installed as `passlib[bcrypt]`'s transitive
dependency, so calling it directly adds nothing new to `requirements.txt`.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import bcrypt
import jwt

from app.core.config import settings

# bcrypt only ever consults the first 72 bytes of the input; bcrypt>=4 raises ValueError instead of
# silently truncating longer input. Truncate explicitly so an unusually long (but otherwise valid)
# password never turns into a 500.
_BCRYPT_MAX_INPUT_BYTES = 72


def _prepare_password_bytes(plain_password: str) -> bytes:
    return plain_password.encode("utf-8")[:_BCRYPT_MAX_INPUT_BYTES]


def hash_password(plain_password: str) -> str:
    """Hash a plaintext password with bcrypt at `settings.bcrypt_rounds` (default cost 12)."""
    salt = bcrypt.gensalt(rounds=settings.bcrypt_rounds)
    digest = bcrypt.hashpw(_prepare_password_bytes(plain_password), salt)
    return digest.decode("utf-8")


def verify_password(plain_password: str, hashed_password: str) -> bool:
    """Verify a plaintext password against its bcrypt hash.

    Never raises: a malformed/foreign hash (e.g. from a corrupted row) is treated as "does not
    match" rather than propagating a 500 out of the login route.
    """
    try:
        return bcrypt.checkpw(_prepare_password_bytes(plain_password), hashed_password.encode("utf-8"))
    except (ValueError, TypeError):
        return False


def create_access_token(subject: str, role: str) -> str:
    """Encode an HS256 JWT for `subject` (the user id, as `str(uuid.UUID)`) carrying a `role` claim.

    Expiry is `settings.jwt_expire_minutes` (default 1440 = 24h) from the moment of issuance.
    """
    now = datetime.now(timezone.utc)
    payload: dict[str, Any] = {
        "sub": subject,
        "role": role,
        "iat": now,
        "exp": now + timedelta(minutes=settings.jwt_expire_minutes),
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)


def decode_access_token(token: str) -> dict[str, Any]:
    """Decode and validate an HS256 JWT, returning its claims (`sub`, `role`, `iat`, `exp`).

    Raises `jwt.PyJWTError` (e.g. `ExpiredSignatureError`, `InvalidSignatureError`,
    `DecodeError`) on an invalid, tampered, or expired token. Callers — see
    `app.api.deps.get_current_user` — catch that and translate it into a 401.
    """
    return jwt.decode(token, settings.jwt_secret, algorithms=[settings.jwt_algorithm])
