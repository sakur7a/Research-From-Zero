"""Create a consistent SQLite backup, including committed WAL contents.

Usage: python scripts/backup.py --output backups/re0-before-upgrade.sqlite3
The destination must not exist. No overwrite or automatic restore is performed.

The implementation lives in `re0.db`, because that is also where the automatic pre-migration copy is
made; a second copy of it here would drift, and the copy that runs before an upgrade is the one that
has to be right.
"""
from __future__ import annotations

import argparse
from pathlib import Path
import os
import sqlite3
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from re0.db import backup_database  # noqa: E402  (re-exported for callers and tests)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=Path(os.getenv("RE0_DB", str(ROOT / ".data/re0.sqlite3"))))
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        result = backup_database(args.source, args.output)
    except (OSError, ValueError, sqlite3.Error) as exc:
        print(f"备份失败：{exc}", file=sys.stderr)
        return 1
    print(f"备份完成：{result}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
