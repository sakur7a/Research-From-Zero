#!/usr/bin/env python3
"""Skill wrapper: find the `re0` package, then delegate.

The retrieval, formatting and verification live in `re0.skill_search`, so this skill and
`re0 paper search` cannot drift apart — this file only locates the package. Both of these run the
same code:

    python paper_search.py --query "layer decomposition" --start-year 2025
    re0 paper search --query "layer decomposition" --start-year 2025
"""
from __future__ import annotations

import os
import sys
from pathlib import Path


def locate_backend():
    """Find the `re0` package, whether this skill sits inside the repository or not.

    A skill copied into an agent's skills directory is no longer at a fixed depth under the
    repository, so `parents[3]` cannot be relied on. `RE0_HOME` names a checkout explicitly;
    otherwise a repository the skill still lives in is tried; otherwise the ambient environment
    is used as-is (an installed `re0-research`). Nothing is searched broadly, so a wrong
    `RE0_HOME` fails loudly instead of picking up an unrelated package.
    """
    explicit = os.getenv("RE0_HOME", "").strip()
    if explicit:
        candidate = Path(explicit).expanduser() / "backend"
        if (candidate / "re0" / "agent" / "tools.py").is_file():
            return candidate
        print(f"RE0_HOME={explicit} does not look like a Re0 checkout "
              "(no backend/re0/agent/tools.py)", file=sys.stderr)
        raise SystemExit(2)
    here = Path(__file__).resolve()
    # <repo>/skills/<skill>/scripts/paper_search.py -> <repo>
    if len(here.parents) > 3:
        candidate = here.parents[3] / "backend"
        if (candidate / "re0" / "agent" / "tools.py").is_file():
            return candidate
    return None


_backend = locate_backend()
if _backend is not None:
    sys.path.insert(0, str(_backend))

try:
    from re0.skill_search import main
except ModuleNotFoundError:
    print("re0 is not importable from here. Either install it (pip install .) or point this skill\n"
          "at a checkout: RE0_HOME=/path/to/re0 python paper_search.py ...", file=sys.stderr)
    raise SystemExit(2)

if __name__ == "__main__":
    raise SystemExit(main())
