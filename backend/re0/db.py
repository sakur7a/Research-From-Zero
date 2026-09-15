"""SQLite storage with transactional writes and immutable check observations."""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL);
INSERT INTO schema_version SELECT 1 WHERE NOT EXISTS (SELECT 1 FROM schema_version);
CREATE TABLE IF NOT EXISTS papers (
  id TEXT PRIMARY KEY, data TEXT NOT NULL,
  doi TEXT NOT NULL DEFAULT '', arxiv_base TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL, is_demo INTEGER NOT NULL DEFAULT 0
);
CREATE UNIQUE INDEX IF NOT EXISTS papers_doi ON papers(doi) WHERE doi != '';
CREATE UNIQUE INDEX IF NOT EXISTS papers_arxiv ON papers(arxiv_base) WHERE arxiv_base != '';
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
CREATE TABLE IF NOT EXISTS topics (name TEXT PRIMARY KEY);
"""


def encode(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


class Database:
    def __init__(self, path: str):
        self.path = path
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as con:
            con.executescript(SCHEMA)
            version = con.execute("SELECT version FROM schema_version").fetchone()[0]
            if version != 1:
                raise RuntimeError(f"Unsupported database schema: {version}")
            con.executemany("INSERT OR IGNORE INTO topics(name) VALUES (?)", [
                ("图层分解 / 生成",), ("Layout 生成",), ("推荐系统",)
            ])

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
