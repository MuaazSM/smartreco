"""Shared FastAPI dependencies: the current-user resolver and the role guard.

`get_current_user` reads the httpOnly JWT cookie set by `POST /api/auth/login`
(`app/api/routes/auth.py`), decodes it via `app.core.security.decode_access_token`, and loads the
corresponding `User` row. `require_role(role)` wraps it to additionally gate on the token's `role`
claim — PRD §6.1: "A FastAPI dependency `require_role("admin")` guards admin routes." Phase 4's
`/api/admin/*` routes depend on this.
"""

from __future__ import annotations

import uuid
from typing import Awaitable, Callable

import jwt
from fastapi import Depends, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import decode_access_token
from app.db.models import User
from app.db.session import get_db

# Shared with app/api/routes/auth.py (which sets it on login/clears it on logout). Kept here, next
# to the code that reads it, so the two stay in sync by construction.
ACCESS_TOKEN_COOKIE_NAME = "access_token"


async def get_current_user(
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> User:
    """Resolve the authenticated `User` from the httpOnly JWT cookie.

    Raises `HTTPException(401)` when the cookie is missing, the token is invalid/expired/tampered,
    or the token's subject no longer names an existing user (e.g. deleted after the token was
    issued).
    """
    token = request.cookies.get(ACCESS_TOKEN_COOKIE_NAME)
    if not token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated")

    try:
        claims = decode_access_token(token)
        user_id = uuid.UUID(str(claims["sub"]))
    except (jwt.PyJWTError, KeyError, ValueError):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or expired token"
        )

    user = await db.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="User no longer exists")
    return user


def require_role(role: str) -> Callable[..., Awaitable[User]]:
    """FastAPI dependency factory: 403s unless the current user's `role` claim equals `role`.

    Usage: `current_user: User = Depends(require_role("admin"))`.
    """

    async def _require_role(current_user: User = Depends(get_current_user)) -> User:
        if current_user.role != role:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Requires role '{role}'",
            )
        return current_user

    return _require_role
