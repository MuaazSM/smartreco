"""Seed the SmartReco catalog + demo accounts (PRD §6.2; IMPLEMENTATION.md Phase 4).

Populates a fresh environment with:
  * **40 course products** across 8 categories and 3 levels;
  * **1 admin** (``admin@smartreco.dev``) for the CRUD + sync-status console;
  * **3 demo users with deliberately divergent behavior histories** — a data/ML learner, a web-dev
    learner, and a cloud/security learner — so the Phase 6/8 recommendations visibly differ per
    persona out of the box, with no manual clicking.

Products are created **through** ``app.services.catalog.create_product`` so every one gets its
transactional ``vector_outbox`` row; the script then calls ``drain_outbox_once`` directly to sync
Qdrant immediately (so ``sync-status`` reports ``in_sync: true`` the moment seeding finishes). Events
are written directly with PRD §6.3 server-side weights (Phase 5's ingest service does not exist yet).

Safe to rerun: by default it resets the catalog/behavior tables and clears the Qdrant collection
first (pass ``--no-reset`` to append instead). Run under ``MESH_DISABLED=true`` so it costs no quota —
embeddings become deterministic offline pseudo-vectors, which is exactly what gets upserted.

Usage:
    MESH_DISABLED=true python scripts/seed_data.py
    MESH_DISABLED=true python scripts/seed_data.py --no-reset
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Allow `python scripts/seed_data.py` from the repo root without PYTHONPATH gymnastics.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import delete, text  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncSession  # noqa: E402

from app.core.security import hash_password  # noqa: E402
from app.db.models import Event, Product, User  # noqa: E402
from app.db.session import AsyncSessionLocal  # noqa: E402
from app.services import catalog  # noqa: E402
from app.services.outbox_worker import drain_outbox_once  # noqa: E402
from app.vector.qdrant_client import QdrantVectorStore  # noqa: E402

ADMIN_EMAIL = "admin@smartreco.dev"
# Local demo credential only (not a real secret — see CLAUDE.md #4); override via env for a deploy.
ADMIN_PASSWORD = os.getenv("SEED_ADMIN_PASSWORD", "smartreco-admin-2026")
DEMO_PASSWORD = os.getenv("SEED_DEMO_PASSWORD", "smartreco-demo-2026")

# PRD §6.3 server-side event weights (view 1.0 · click 1.5 · search 2.0 · dwell>30s 2.5 · cart 3.0).
EVENT_WEIGHTS: dict[str, float] = {
    "view": 1.0,
    "click": 1.5,
    "search": 2.0,
    "dwell": 2.5,
    "cart": 3.0,
}

# 40 courses. (title, category, level, price_cents, tags)
COURSES: list[tuple[str, str, str, int, list[str]]] = [
    # --- Data Science ---
    ("Python for Data Analysis", "Data Science", "beginner", 4900, ["python", "pandas", "data"]),
    ("Statistics Foundations for Data Science", "Data Science", "beginner", 3900, ["statistics", "probability"]),
    ("Data Visualization with Matplotlib & Seaborn", "Data Science", "intermediate", 5900, ["visualization", "python"]),
    ("SQL for Data Analysts", "Data Science", "beginner", 4500, ["sql", "databases", "analytics"]),
    ("Feature Engineering Masterclass", "Data Science", "advanced", 8900, ["features", "python", "ml"]),
    ("Time Series Forecasting in Python", "Data Science", "advanced", 9900, ["time-series", "forecasting", "python"]),
    # --- Machine Learning ---
    ("Machine Learning Fundamentals", "Machine Learning", "beginner", 5900, ["ml", "scikit-learn", "python"]),
    ("Deep Learning with PyTorch", "Machine Learning", "intermediate", 9900, ["deep-learning", "pytorch", "neural-networks"]),
    ("Applied Reinforcement Learning", "Machine Learning", "advanced", 12900, ["rl", "reinforcement-learning"]),
    ("Model Deployment & MLOps", "Machine Learning", "advanced", 11900, ["mlops", "deployment", "docker"]),
    ("Natural Language Processing Basics", "Machine Learning", "intermediate", 7900, ["nlp", "text", "python"]),
    ("Computer Vision with CNNs", "Machine Learning", "advanced", 10900, ["computer-vision", "cnn", "pytorch"]),
    # --- AI & LLMs ---
    ("Building RAG Applications", "AI & LLMs", "intermediate", 8900, ["rag", "llm", "vector-db", "retrieval"]),
    ("Advanced Retrieval-Augmented Generation", "AI & LLMs", "advanced", 12900, ["rag", "reranking", "hybrid-search"]),
    ("Prompt Engineering in Practice", "AI & LLMs", "beginner", 4900, ["prompting", "llm", "gpt"]),
    ("LangGraph Agents from Scratch", "AI & LLMs", "advanced", 13900, ["agents", "langgraph", "orchestration"]),
    ("Fine-Tuning Large Language Models", "AI & LLMs", "advanced", 14900, ["fine-tuning", "llm", "training"]),
    ("Vector Databases Deep Dive", "AI & LLMs", "intermediate", 7900, ["vector-db", "embeddings", "qdrant"]),
    # --- Web Development ---
    ("Modern HTML & CSS", "Web Development", "beginner", 2900, ["html", "css", "frontend"]),
    ("JavaScript: The Complete Guide", "Web Development", "beginner", 4900, ["javascript", "es6", "frontend"]),
    ("React from Zero to Hero", "Web Development", "intermediate", 6900, ["react", "javascript", "frontend"]),
    ("Next.js App Router in Depth", "Web Development", "intermediate", 7900, ["nextjs", "react", "ssr"]),
    ("TypeScript for Professionals", "Web Development", "intermediate", 5900, ["typescript", "javascript"]),
    ("Full-Stack with Node & Express", "Web Development", "intermediate", 8900, ["node", "express", "backend"]),
    ("Building REST APIs with FastAPI", "Web Development", "intermediate", 6900, ["fastapi", "python", "api"]),
    ("Web Performance Optimization", "Web Development", "advanced", 9900, ["performance", "frontend", "web-vitals"]),
    # --- Cloud & DevOps ---
    ("Docker & Containers Essentials", "Cloud & DevOps", "beginner", 4900, ["docker", "containers", "devops"]),
    ("Kubernetes for Developers", "Cloud & DevOps", "intermediate", 8900, ["kubernetes", "k8s", "orchestration"]),
    ("AWS Cloud Practitioner", "Cloud & DevOps", "beginner", 5900, ["aws", "cloud"]),
    ("Terraform Infrastructure as Code", "Cloud & DevOps", "intermediate", 7900, ["terraform", "iac", "devops"]),
    ("CI/CD with GitHub Actions", "Cloud & DevOps", "intermediate", 5900, ["ci-cd", "github-actions", "automation"]),
    ("Site Reliability Engineering", "Cloud & DevOps", "advanced", 11900, ["sre", "observability", "reliability"]),
    # --- Programming ---
    ("Clean Code & Refactoring", "Programming", "intermediate", 5900, ["clean-code", "refactoring", "craft"]),
    ("Data Structures & Algorithms", "Programming", "intermediate", 6900, ["algorithms", "data-structures"]),
    ("Rust for Systems Programming", "Programming", "advanced", 9900, ["rust", "systems"]),
    ("Go Concurrency Patterns", "Programming", "advanced", 8900, ["golang", "concurrency"]),
    # --- Cybersecurity ---
    ("Web Application Security", "Cybersecurity", "intermediate", 7900, ["security", "owasp", "web"]),
    ("Ethical Hacking Fundamentals", "Cybersecurity", "beginner", 5900, ["ethical-hacking", "pentest"]),
    # --- Design ---
    ("UX Design Principles", "Design", "beginner", 4900, ["ux", "design", "usability"]),
    ("Figma for Product Designers", "Design", "beginner", 3900, ["figma", "design", "ui"]),
]

# Personas: (email, display_name, categories they gravitate to, search terms they use).
PERSONAS: list[tuple[str, str, list[str], list[str]]] = [
    (
        "ana@smartreco.dev",
        "Ana (Data/ML)",
        ["Data Science", "Machine Learning", "AI & LLMs"],
        ["machine learning", "deep learning pytorch", "rag pipeline", "time series"],
    ),
    (
        "ben@smartreco.dev",
        "Ben (Web Dev)",
        ["Web Development", "Programming"],
        ["react hooks", "typescript generics", "fastapi rest api", "next.js"],
    ),
    (
        "cara@smartreco.dev",
        "Cara (Cloud/Security)",
        ["Cloud & DevOps", "Cybersecurity"],
        ["kubernetes", "terraform", "ci cd pipeline", "web security owasp"],
    ),
]

_RESET_TABLES = (
    "events",
    "recommendations",
    "agent_runs",
    "user_profiles",
    "vector_outbox",
    "products",
)


async def _reset(session: AsyncSession) -> None:
    """Truncate catalog/behavior tables, drop seed users, and clear the Qdrant collection."""
    await session.execute(
        text(f"TRUNCATE {', '.join(_RESET_TABLES)} RESTART IDENTITY CASCADE")
    )
    seed_emails = [ADMIN_EMAIL] + [p[0] for p in PERSONAS]
    await session.execute(delete(User).where(User.email.in_(seed_emails)))
    await session.commit()

    store = QdrantVectorStore()
    try:
        await store.clear_all()
    finally:
        await store.close()


async def _seed_users(session: AsyncSession) -> dict[str, User]:
    """Create the admin + 3 demo users, returning them keyed by email."""
    users: dict[str, User] = {}

    admin = User(
        email=ADMIN_EMAIL,
        password_hash=hash_password(ADMIN_PASSWORD),
        display_name="SmartReco Admin",
        role="admin",
    )
    session.add(admin)
    users[ADMIN_EMAIL] = admin

    for email, display_name, _cats, _terms in PERSONAS:
        user = User(
            email=email,
            password_hash=hash_password(DEMO_PASSWORD),
            display_name=display_name,
            role="user",
        )
        session.add(user)
        users[email] = user

    await session.commit()
    for user in users.values():
        await session.refresh(user)
    return users


async def _seed_products(session: AsyncSession) -> list[Product]:
    """Create all 40 courses through the catalog service (so each gets an outbox row)."""
    products: list[Product] = []
    for title, category, level, price_cents, tags in COURSES:
        product = await catalog.create_product(
            session,
            title=title,
            description=(
                f"{title}: a hands-on {level} course in {category}. "
                f"Covers {', '.join(tags)} with projects and real-world examples."
            ),
            category=category,
            tags=tags,
            level=level,
            price_cents=price_cents,
        )
        products.append(product)
    return products


def _build_persona_events(
    user: User, categories: list[str], search_terms: list[str], products: list[Product]
) -> list[Event]:
    """A divergent behavior history: searches + views/clicks/dwell/cart concentrated on the persona's
    categories, plus a little cross-category noise so the profile is realistic rather than a monoculture.
    """
    session_id = uuid.uuid4()
    now = datetime.now(timezone.utc)
    tick = 0

    def next_ts() -> datetime:
        nonlocal tick
        tick += 1
        # Spread events over the last ~3 days, each with a unique client_ts (natural-key safe).
        return now - timedelta(hours=tick * 2, minutes=tick)

    events: list[Event] = []

    def add(event_type: str, product: Product | None, payload: dict | None = None) -> None:
        events.append(
            Event(
                user_id=user.id,
                session_id=session_id,
                event_type=event_type,
                product_id=product.id if product is not None else None,
                payload=payload or {},
                weight=EVENT_WEIGHTS[event_type],
                client_ts=next_ts(),
            )
        )

    in_focus = [p for p in products if p.category in categories]
    off_focus = [p for p in products if p.category not in categories]

    # Searches (high signal) for the persona's terms.
    for term in search_terms:
        add("search", None, {"query": term})

    # Strong engagement with in-focus courses: view -> click -> dwell, and cart the top few.
    for index, product in enumerate(in_focus[:8]):
        add("view", product)
        add("click", product)
        if index < 5:
            add("dwell", product, {"dwell_ms": 45000})
        if index < 3:
            add("cart", product)

    # A little cross-category noise (just views) so the centroid isn't a pure one-category vector.
    for product in off_focus[:2]:
        add("view", product)

    return events


async def _seed_events(session: AsyncSession, users: dict[str, User], products: list[Product]) -> int:
    """Write each persona's divergent event history. Returns the total number of events inserted."""
    total = 0
    for email, _display, categories, terms in PERSONAS:
        events = _build_persona_events(users[email], categories, terms, products)
        session.add_all(events)
        total += len(events)
    await session.commit()
    return total


async def main(reset: bool) -> None:
    async with AsyncSessionLocal() as session:
        if reset:
            await _reset(session)
            print("reset: truncated catalog/behavior tables + cleared Qdrant collection")

        users = await _seed_users(session)
        print(f"users: 1 admin ({ADMIN_EMAIL}) + {len(PERSONAS)} demo personas")

        products = await _seed_products(session)
        print(f"products: created {len(products)} courses (each with a pending outbox row)")

        event_count = await _seed_events(session, users, products)
        print(f"events: wrote {event_count} pre-baked behavior events across {len(PERSONAS)} personas")

    # Drain the outbox so Qdrant is populated immediately (sync-status -> in_sync right after seeding).
    drained = 0
    store = QdrantVectorStore()
    try:
        while True:
            n = await drain_outbox_once(vector_store=store, limit=100)
            drained += n
            if n == 0:
                break
        point_count = await store.count()
    finally:
        await store.close()

    print(f"outbox: drained {drained} rows -> Qdrant now holds {point_count} points")
    print("done. Admin login:")
    print(f"  email={ADMIN_EMAIL}  password={ADMIN_PASSWORD}")
    print(f"  demo users password={DEMO_PASSWORD}  ({', '.join(p[0] for p in PERSONAS)})")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Seed SmartReco catalog + demo accounts.")
    parser.add_argument(
        "--no-reset",
        dest="reset",
        action="store_false",
        help="append instead of truncating existing catalog/behavior data first",
    )
    parser.set_defaults(reset=True)
    args = parser.parse_args()
    asyncio.run(main(reset=args.reset))
