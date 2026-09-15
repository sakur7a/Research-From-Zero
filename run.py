"""Run from the repository root: python run.py."""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "backend"))

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("re0.main:app", host=os.getenv("RE0_HOST", "127.0.0.1"),
                port=int(os.getenv("RE0_PORT", "8000")))
