"""Auth routes (PRD §6.1, §8): register, login (sets an httpOnly JWT cookie), me.

Simple email/password auth, no OAuth (PRD §6.1 non-goal). Passwords are bcrypt-hashed
(`app.core.security`) and never leave this module in a response — every route returns `UserOut`,
which has no `password_hash` field. Login mints an HS256 JWT (`role` claim, 24h expiry) and sets it
as an httpOnly cookie; `SameSite`/`Secure` are derived from `settings.cookie_samesite`
(CLAUDE.md/PRD §13.2 — `lax` for same-origin local dev, `none` + `Secure` for a cross-origin deploy).
"""

from __future__ import annotations

import re
import uuid
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import ACCESS_TOKEN_COOKIE_NAME, get_current_user, require_role
from app.core.config import settings
from app.core.logging import get_logger
from app.core.security import create_access_token, hash_password, verify_password
from app.db.models import User
from app.db.session import get_db

logger = get_logger(__name__)

router = APIRouter(prefix="/api/auth", tags=["auth"])

# Deliberately not `pydantic.EmailStr`: that type requires the optional `email-validator` package,
# which is not in requirements.txt (CLAUDE.md — "don't add dependencies without a one-sentence
# reason", and this phase owns no requirements.txt changes). This regex is a pragmatic shape check;
# Postgres's `CITEXT` column + unique constraint on `users.email` is the real source of truth for
# uniqueness and case-insensitivity.
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class RegisterRequest(BaseModel):
    email: str = Field(..., max_length=320)
    password: str = Field(..., min_length=8, max_length=128)
    display_name: str = Field(..., min_length=1, max_length=200)

    @field_validator("email")
    @classmethod
    def _valid_email_shape(cls, value: str) -> str:
        if not _EMAIL_RE.match(value):
            raise ValueError("not a valid email address")
        return value.strip().lower()


class LoginRequest(BaseModel):
    email: str
    password: str


class UserOut(BaseModel):
    """Public user shape. No `password_hash` field — this is what makes it safe to return."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    email: str
    display_name: str
    role: str
    digest_optin: bool
    created_at: datetime


def _cookie_kwargs() -> dict[str, Any]:
    samesite = settings.cookie_samesite.lower()
    return {
        "httponly": True,
        "samesite": samesite,
        # SameSite=None cookies are rejected by browsers unless Secure is also set.
        "secure": samesite == "none",
        "max_age": settings.jwt_expire_minutes * 60,
        "path": "/",
    }


@router.post("/register", response_model=UserOut, status_code=status.HTTP_201_CREATED)
async def register(body: RegisterRequest, db: AsyncSession = Depends(get_db)) -> User:
    """Create a new user. Role is always `user` — nothing here lets a caller self-promote to admin."""
    existing = await db.scalar(select(User).where(User.email == body.email))
    if existing is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="Email already registered"
        )

    user = User(
        email=body.email,
        password_hash=hash_password(body.password),
        display_name=body.display_name,
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)
    logger.info("auth.register", extra={"extra_fields": {"user_id": str(user.id)}})
    return user


@router.post("/login", response_model=UserOut)
async def login(
    body: LoginRequest, response: Response, db: AsyncSession = Depends(get_db)
) -> User:
    """Verify credentials and set the httpOnly JWT cookie. Generic 401 on any mismatch —
    never reveals whether the email or the password was wrong."""
    user = await db.scalar(select(User).where(User.email == body.email))
    if user is None or not verify_password(body.password, user.password_hash):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid email or password"
        )

    token = create_access_token(subject=str(user.id), role=user.role)
    response.set_cookie(ACCESS_TOKEN_COOKIE_NAME, token, **_cookie_kwargs())
    logger.info("auth.login", extra={"extra_fields": {"user_id": str(user.id)}})
    return user


@router.post("/logout")
async def logout(response: Response) -> dict[str, str]:
    """End the session by clearing the httpOnly JWT cookie. The cookie can't be cleared from JS, so
    a real logout must happen server-side; the delete must reuse the same path/SameSite/Secure the
    cookie was set with or the browser won't drop it. Idempotent — safe to call when already logged
    out (no auth required)."""
    samesite = settings.cookie_samesite.lower()
    response.delete_cookie(
        ACCESS_TOKEN_COOKIE_NAME,
        path="/",
        samesite=samesite,
        secure=samesite == "none",
        httponly=True,
    )
    logger.info("auth.logout")
    return {"detail": "logged out"}


@router.get("/me", response_model=UserOut)
async def me(current_user: User = Depends(get_current_user)) -> User:
    return current_user


@router.get("/_admin_probe", response_model=UserOut)
async def admin_probe(current_user: User = Depends(require_role("admin"))) -> User:
    """Exists only to exercise `require_role("admin")` end-to-end (`tests/test_auth.py`) before
    Phase 4 adds the real `/api/admin/*` routes. Adds no state — safe to delete once those land."""
    return current_user
