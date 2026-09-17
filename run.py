"""Run from the repository root: python run.py.

`RE0_ENV_FILE` is honoured here so the web UI and the retrieval skill can share one
credential file. It is opt-in: with the variable unset nothing is read, and a variable
that is already set in the environment always wins over the file.
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "backend"))

if __name__ == "__main__":
    from re0.env_file import load

    env_file = os.getenv("RE0_ENV_FILE", "").strip()
    if env_file:
        applied = load(env_file)
        # Names only: a value must never reach a terminal, log or task record.
        print(f"loaded {len(applied)} variables from {env_file}" if applied
              else f"no usable variables in {env_file}")

    import uvicorn
    uvicorn.run("re0.main:app", host=os.getenv("RE0_HOST", "127.0.0.1"),
                port=int(os.getenv("RE0_PORT", "8000")))
