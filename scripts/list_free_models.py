"""Print the free-tier model catalog Mesh resolves at startup (PRD §6.5.1 rule 1).

Paste this output into the README to prove per-node routing is real and to document exactly which
models the submission was judged on. A thin wrapper over ``model_router.refresh_free_models()`` (which
itself calls Mesh ``GET /v1/models``). Under ``MESH_DISABLED=true`` it makes **no** network call and
prints the deterministic fixture catalog instead, so it degrades gracefully offline.

Run:  MESH_DISABLED=true python scripts/list_free_models.py   # offline fixture catalog
      python scripts/list_free_models.py                      # live Mesh free catalog
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

# Allow `python scripts/list_free_models.py` from the repo root without PYTHONPATH gymnastics.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.config import settings  # noqa: E402  (after sys.path bootstrap)
from app.llm.model_router import (  # noqa: E402
    FREE_PREFERENCE,
    PAID_FALLBACK,
    refresh_free_models,
)


async def main() -> None:
    if settings.mesh_disabled:
        print("MESH_DISABLED=true — no network call; printing the fixture free catalog:")
        for model_id in FREE_PREFERENCE:
            print(f"  [free*] {model_id}")
        print(f"  paid fallback: {PAID_FALLBACK}")
        print("(* fixture; the real catalog resolves from Mesh /v1/models when MESH_DISABLED=false)")
        return

    free = await refresh_free_models()
    print(f"Resolved {len(free)} free-tier models from Mesh /v1/models:")
    for model_id in sorted(free):
        marker = "*" if model_id in FREE_PREFERENCE else " "
        print(f"  [free]{marker} {model_id}")
    print(f"  paid fallback: {PAID_FALLBACK}")
    print("(* = in the routing preference list, app/llm/model_router.py::FREE_PREFERENCE)")


if __name__ == "__main__":
    asyncio.run(main())
