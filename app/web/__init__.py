"""Server-rendered Jinja2 frontend (the challenge's suggested stack: FastAPI + Jinja2 templates + JS
tracking). Mounted by ``app.main`` alongside the JSON API; the polished Next.js app (``frontend/``,
deployed to Vercel) remains the primary UI, and this surface demonstrates the templated stack on the
same backend, reusing the exact same ``/api/*`` endpoints for auth, events, feedback, and refresh."""
