"""HTML routes for the server-rendered Jinja2 frontend.

These render templates from ``app/web/templates`` and read the same Postgres rows the JSON API serves
(active products, the user's ``is_current`` recommendation). Writes — login, event batches, feedback,
refresh — are *not* duplicated here: the templates' small scripts call the existing ``/api/*``
endpoints (same origin, so the httpOnly ``access_token`` cookie just works). Auth on a page is
optional: ``_optional_user`` returns the logged-in ``User`` or ``None`` so a page can render
logged-out or redirect. Nothing here constructs an LLM/vector client — pure reads + template render.
"""

from __future__ import annotations

import uuid
from pathlib import Path

import jwt
from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import ACCESS_TOKEN_COOKIE_NAME
from app.core.security import decode_access_token
from app.db.models import Product, User
from app.db.models import Recommendation as RecommendationRow
from app.db.session import get_db

_WEB_DIR = Path(__file__).parent
WEB_STATIC_DIR = _WEB_DIR / "static"

templates = Jinja2Templates(directory=str(_WEB_DIR / "templates"))


def _format_price(cents: int | None) -> str:
    if not cents or cents <= 0:
        return "Free"
    return f"${cents / 100:.2f}"


templates.env.filters["price"] = _format_price

router = APIRouter(tags=["web"], include_in_schema=False)

# Mirrors the seed taxonomy (scripts/seed_data.py) — a lightweight filter, not a live lookup.
CATEGORIES = [
    "Data Science",
    "Machine Learning",
    "AI & LLMs",
    "Web Development",
    "Cloud & DevOps",
    "Programming",
    "Cybersecurity",
    "Design",
]


async def _optional_user(request: Request, db: AsyncSession) -> User | None:
    """The logged-in user from the httpOnly JWT cookie, or ``None`` — never raises (unlike the API's
    ``get_current_user``), so a page can render a logged-out state or redirect itself."""
    token = request.cookies.get(ACCESS_TOKEN_COOKIE_NAME)
    if not token:
        return None
    try:
        claims = decode_access_token(token)
        user_id = uuid.UUID(str(claims["sub"]))
    except (jwt.PyJWTError, KeyError, ValueError):
        return None
    return await db.get(User, user_id)


@router.get("/", response_class=HTMLResponse)
async def index(request: Request, db: AsyncSession = Depends(get_db)) -> HTMLResponse:
    user = await _optional_user(request, db)
    return templates.TemplateResponse(request, "index.html.j2", {"user": user})


@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request, db: AsyncSession = Depends(get_db)) -> HTMLResponse:
    user = await _optional_user(request, db)
    if user is not None:
        return RedirectResponse("/dashboard", status_code=303)
    return templates.TemplateResponse(request, "login.html.j2", {"user": None})


@router.get("/register", response_class=HTMLResponse)
async def register_page(request: Request, db: AsyncSession = Depends(get_db)) -> HTMLResponse:
    user = await _optional_user(request, db)
    if user is not None:
        return RedirectResponse("/dashboard", status_code=303)
    return templates.TemplateResponse(request, "register.html.j2", {"user": None})


@router.get("/catalog", response_class=HTMLResponse)
async def catalog(
    request: Request,
    q: str = "",
    category: str = "",
    db: AsyncSession = Depends(get_db),
) -> HTMLResponse:
    user = await _optional_user(request, db)
    stmt = select(Product).where(Product.is_active.is_(True))
    if category:
        stmt = stmt.where(Product.category == category)
    if q:
        like = f"%{q}%"
        stmt = stmt.where(or_(Product.title.ilike(like), Product.description.ilike(like)))
    stmt = stmt.order_by(Product.category, Product.title).limit(60)
    products = (await db.scalars(stmt)).all()
    return templates.TemplateResponse(
        request,
        "catalog.html.j2",
        {"user": user, "products": products, "q": q, "category": category, "categories": CATEGORIES},
    )


@router.get("/product/{product_id}", response_class=HTMLResponse)
async def product_detail(
    product_id: uuid.UUID, request: Request, db: AsyncSession = Depends(get_db)
) -> HTMLResponse:
    user = await _optional_user(request, db)
    product = await db.get(Product, product_id)
    if product is None or not product.is_active:
        return templates.TemplateResponse(
            request, "product.html.j2", {"user": user, "product": None}, status_code=404
        )
    return templates.TemplateResponse(request, "product.html.j2", {"user": user, "product": product})


@router.get("/dashboard", response_class=HTMLResponse)
async def dashboard(request: Request, db: AsyncSession = Depends(get_db)) -> HTMLResponse:
    user = await _optional_user(request, db)
    if user is None:
        return RedirectResponse("/login", status_code=303)
    rec = await db.scalar(
        select(RecommendationRow).where(
            RecommendationRow.user_id == user.id,
            RecommendationRow.is_current.is_(True),
        )
    )
    return templates.TemplateResponse(request, "dashboard.html.j2", {"user": user, "rec": rec})
