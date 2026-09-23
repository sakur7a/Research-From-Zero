"""Restore a SQLite backup to a new database file without overwriting existing data.

Usage: python scripts/restore.py --source backups/re0.sqlite3 --destination .data/re0-restored.sqlite3
Stop the service before restoring, then review the new file and point RE0_DB at it. The original
database and the backup both remain untouched for rollback.
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from re0.db import restore_database  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True, help="SQLite backup to restore")
    parser.add_argument("--destination", type=Path, required=True,
                        help="new database path; an existing file is never overwritten")
    args = parser.parse_args()
    try:
        result = restore_database(args.source, args.destination)
    except (OSError, ValueError, sqlite3.Error) as exc:
        print(f"恢复失败：{exc}", file=sys.stderr)
        return 1
    print(f"恢复完成（已验证）：{result}")
    print("原始数据库未修改；核对结果后再将服务的 RE0_DB 指向此文件。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
