"""Fire the SmartReco daily digest on demand (PRD §6.7 — "so you can demo it without waiting for
09:00"). Talks straight to ``app.scheduler.digest`` / ``app.scheduler.jobs``; does not touch the
APScheduler cron or the GitHub Actions workflow at all.

Same graceful-degradation contract as the scheduled job: with no ``SMTP_*``/``RESEND_API_KEY``
configured (the default local ``.env``), the rendered HTML is saved to disk instead of sent, and the
path is printed — never a crash.

Usage:
    MESH_DISABLED=true ./.venv/bin/python scripts/send_digest_now.py
        # every opted-in user active in the last 24h (same recipient set as the 09:00 job)

    MESH_DISABLED=true ./.venv/bin/python scripts/send_digest_now.py --email demo@smartreco.dev
        # one user by email, ignoring the opt-in/24h-activity gate (handy right after seeding)

    MESH_DISABLED=true ./.venv/bin/python scripts/send_digest_now.py --save-dir /tmp/digests
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

# Allow `python scripts/send_digest_now.py` from the repo root without PYTHONPATH gymnastics.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import select  # noqa: E402

from app.core.config import settings  # noqa: E402
from app.core.logging import configure_logging, get_logger  # noqa: E402
from app.db.models import User  # noqa: E402
from app.db.session import AsyncSessionLocal  # noqa: E402
from app.scheduler import digest, jobs  # noqa: E402

logger = get_logger(__name__)


async def _resolve_users(email: str | None) -> list[User]:
    if email:
        async with AsyncSessionLocal() as session:
            user = (
                await session.execute(select(User).where(User.email == email))
            ).scalar_one_or_none()
        return [user] if user is not None else []
    return await jobs.opted_in_active_user_ids(AsyncSessionLocal)


async def _run(email: str | None, save_dir: Path | None) -> int:
    users = await _resolve_users(email)
    if not users:
        if email:
            print(f"No user found with email {email!r}.", file=sys.stderr)
            return 1
        print(
            "No opted-in users active in the last 24h. "
            "Try `--email <address>` to target one user directly (bypasses the opt-in/24h gate)."
        )
        return 0

    sent_count = 0
    for user in users:
        result = await digest.send_digest_for_user(user, save_dir=save_dir)
        if result.skipped:
            print(f"[skip] {user.email}: {result.skip_reason}")
            continue
        if result.sent:
            print(f"[sent via {result.channel}] {user.email} — {result.headline!r}")
            sent_count += 1
        else:
            print(
                f"[not sent — no SMTP/Resend configured] {user.email} — "
                f"rendered HTML saved to {result.saved_path}"
            )

    print(f"\nDone. {sent_count}/{len(users)} delivered.")
    return 0


def main() -> None:
    configure_logging(settings.log_level)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--email", default=None, help="Send to a single user by email (ignores the opt-in/24h gate)."
    )
    parser.add_argument(
        "--save-dir",
        default=None,
        help="Where to save rendered HTML when it can't be emailed (default: system temp dir).",
    )
    args = parser.parse_args()
    save_dir = Path(args.save_dir) if args.save_dir else None
    exit_code = asyncio.run(_run(args.email, save_dir))
    raise SystemExit(exit_code)


if __name__ == "__main__":
    main()
