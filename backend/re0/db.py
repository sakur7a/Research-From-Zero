"""SQLite storage with transactional writes, immutable check observations and versioned migrations.

The library schema is versioned the same way the agent schema is: a fresh database is created at the
current version, an older one is migrated forward, and a *newer* one is refused rather than
downgraded — a database written by a later build holds columns this code cannot interpret, and
guessing at them is how rows get rewritten badly.

Version 2 adds `owner`. Until then a single-user assumption was baked into the tables themselves:
`papers_doi` was globally unique, so one reader's DOI blocked another's import, and the refusal
message ("already in the library") leaked that somebody else had it. Ownership is a column, not a
query convention, and the unique indexes are per owner for the same reason.

A migration copies the database first, through the SQLite backup API, and refuses to proceed if that
copy fails. The upgrade is additive and reversible by restoring the copy; an upgrade with no way back
is not an upgrade.
"""
from __future__ import annotations

import json
import os
import sqlite3
from contextlib import closing, contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from .deployment import LOCAL_OWNER

SCHEMA_TARGET = 2

SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL);
INSERT INTO schema_version SELECT 1 WHERE NOT EXISTS (SELECT 1 FROM schema_version);
CREATE TABLE IF NOT EXISTS papers (
  id TEXT PRIMARY KEY, data TEXT NOT NULL,
  doi TEXT NOT NULL DEFAULT '', arxiv_base TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL, is_demo INTEGER NOT NULL DEFAULT 0,
  owner TEXT NOT NULL DEFAULT 'local'
);
CREATE TABLE IF NOT EXISTS resources (
  id TEXT PRIMARY KEY, paper_id TEXT NOT NULL REFERENCES papers(id) ON DELETE CASCADE,
  data TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS resources_paper ON resources(paper_id);
CREATE TABLE IF NOT EXISTS observations (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  resource_id TEXT NOT NULL REFERENCES resources(id) ON DELETE CASCADE,
  data TEXT NOT NULL, checked_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS observations_resource ON observations(resource_id, id DESC);
CREATE TABLE IF NOT EXISTS topics (
  name TEXT NOT NULL, owner TEXT NOT NULL DEFAULT 'local', PRIMARY KEY (owner, name)
);
"""

# Created after the migration has run, because a v1 database does not have the columns they name.
INDEXES_V2 = """
CREATE UNIQUE INDEX IF NOT EXISTS papers_owner_doi ON papers(owner, doi) WHERE doi != '';
CREATE UNIQUE INDEX IF NOT EXISTS papers_owner_arxiv ON papers(owner, arxiv_base) WHERE arxiv_base != '';
CREATE INDEX IF NOT EXISTS papers_owner ON papers(owner, updated_at DESC, id);
"""

# The three a fresh local library starts with. Seeded for the local owner only: a hosted account
# begins with its own vocabulary rather than inheriting a stranger's.
DEFAULT_TOPICS = ("图层分解 / 生成", "Layout 生成", "推荐系统")

MIGRATION_NOTE = ("papers schema v1→v2：为 papers 与 topics 增加 owner，既有行归入 'local'，"
                  "并把全局唯一的 DOI/arXiv 索引换成按所有者唯一。没有删除任何行，也没有改写 data。")


def encode(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def backup_database(source: Path, destination: Path) -> Path:
    """A consistent copy, including committed WAL contents, through the SQLite backup API.

    The destination must not exist: overwriting the one backup that could have saved the upgrade is
    the failure mode this refuses.
    """
    source = source.expanduser().resolve()
    destination = destination.expanduser().absolute()
    if not source.is_file():
        raise FileNotFoundError(f"数据库不存在，未创建空库：{source}")
    if source == destination.resolve():
        raise ValueError("备份目标不能是源数据库")
    destination.parent.mkdir(parents=True, exist_ok=True)
    # Exclusive creation refuses existing files and symlinks, and preserves older backups.
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


class Database:
    def __init__(self, path: str):
        self.path = path
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.backup = self._pre_migration_backup()
        with self.connect() as con:
            con.executescript(SCHEMA)
            version = con.execute("SELECT version FROM schema_version").fetchone()[0]
            if version > SCHEMA_TARGET:
                raise RuntimeError(f"Unsupported database schema: {version}; no destructive "
                                   "migration was performed")
            if version < SCHEMA_TARGET:
                self._migrate(con, version)
            con.executescript(INDEXES_V2)
            con.executemany("INSERT OR IGNORE INTO topics(name, owner) VALUES (?,?)",
                            [(name, LOCAL_OWNER) for name in DEFAULT_TOPICS])

    def _pre_migration_backup(self) -> Path | None:
        """Copy the database before touching it, but only when there is something to upgrade.

        Read-only, and before any write: a migration that fails halfway leaves the original where it
        was, and the copy beside it.
        """
        path = Path(self.path)
        if not path.is_file():
            return None
        with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as con:
            row = con.execute("SELECT name FROM sqlite_master WHERE type='table' "
                              "AND name='schema_version'").fetchone()
            if row is None:
                return None
            version = con.execute("SELECT version FROM schema_version").fetchone()[0]
        if version >= SCHEMA_TARGET:
            return None
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        destination = path.with_name(f"{path.name}.pre-v{SCHEMA_TARGET}-{stamp}.sqlite3")
        return backup_database(path, destination)

    def _migrate(self, con: sqlite3.Connection, version: int) -> None:
        if version < 2:
            columns = {row[1] for row in con.execute("PRAGMA table_info(papers)")}
            if "owner" not in columns:
                # Every row a single-user database holds belonged to the one user there was. Naming
                # that owner `local` is what keeps it away from an account created later.
                con.execute("ALTER TABLE papers ADD COLUMN owner TEXT NOT NULL DEFAULT 'local'")
            con.execute("DROP INDEX IF EXISTS papers_doi")
            con.execute("DROP INDEX IF EXISTS papers_arxiv")
            topic_columns = {row[1] for row in con.execute("PRAGMA table_info(topics)")}
            if "owner" not in topic_columns:
                # A primary key cannot be altered, so the table is rebuilt and renamed. The copy is
                # made before the drop, and the drop happens only after the copy succeeded.
                con.execute("CREATE TABLE topics_migrated (name TEXT NOT NULL, owner TEXT NOT NULL "
                            "DEFAULT 'local', PRIMARY KEY (owner, name))")
                con.execute("INSERT INTO topics_migrated(name, owner) SELECT name, 'local' FROM topics")
                moved = con.execute("SELECT COUNT(*) FROM topics_migrated").fetchone()[0]
                expected = con.execute("SELECT COUNT(*) FROM topics").fetchone()[0]
                if moved != expected:
                    raise sqlite3.DatabaseError(
                        f"topics 迁移数量不符（{moved} != {expected}）；未删除原表")
                con.execute("DROP TABLE topics")
                con.execute("ALTER TABLE topics_migrated RENAME TO topics")
            con.execute("UPDATE schema_version SET version=?", (SCHEMA_TARGET,))
        broken = con.execute("PRAGMA foreign_key_check").fetchall()
        if broken:
            # Reported rather than ignored: a migration that leaves a dangling reference has already
            # changed the file, and the backup is the way back.
            raise sqlite3.DatabaseError(f"迁移后外键不完整（{len(broken)} 处）；请从迁移前的备份恢复")

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        con = sqlite3.connect(self.path, timeout=15)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA foreign_keys=ON")
        con.execute("PRAGMA journal_mode=WAL")
        try:
            yield con
            con.commit()
        except Exception:
            con.rollback()
            raise
        finally:
            con.close()
