"""Run from the repository root: python run.py.

The logic lives in `re0.serve` so that an installed distribution can start the same server with
`re0 serve`; this file only makes `backend/` importable from a checkout and gets out of the way.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "backend"))

from re0.serve import LOOPBACK_HOSTS, check_deployment, main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
