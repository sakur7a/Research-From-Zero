"""`python -m re0` — the same entry points as the `re0` console script.

The console script only exists once the distribution is installed, which needs a build backend
(setuptools) present. Running from a checkout is a normal thing to want, so the module is
reachable directly:

    PYTHONPATH=backend python -m re0 doctor
"""
from re0.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
