"""Create a consistent SQLite backup, including committed WAL contents.

Usage: python scripts/backup.py --output backups/re0-before-upgrade.sqlite3
The destination must not exist. No overwrite or automatic restore is performed.
"""
from __future__ import annotations

import argparse
from contextlib import closing
import os
from pathlib import Path
import sqlite3
import sys

ROOT = Path(__file__).resolve().parents[1]


def backup_database(source: Path, destination: Path) -> Path:
    source = source.expanduser().resolve()
    destination = destination.expanduser().absolute()
    if not source.is_file():
        raise FileNotFoundError(f"数据库不存在，未创建空库：{source}")
    if source == destination.resolve():
        raise ValueError("备份目标不能是源数据库")
    destination.parent.mkdir(parents=True, exist_ok=True)
    # Exclusive creation refuses existing files/symlinks and preserves old backups.
    descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    os.close(descriptor)
    try:
        with closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)) as source_db:
            with closing(sqlite3.connect(destination)) as target_db:
                source_db.backup(target_db)
                if target_db.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                    raise sqlite3.DatabaseError("备份完整性检查未通过")
    except Exception:
        destination.unlink(missing_ok=True)
        raise
    return destination


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
