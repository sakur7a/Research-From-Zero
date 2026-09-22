"""Read-only, incremental Zotero sync with an explicit source boundary.

A user names one library, previews what would change, and only then is anything written. Three
things this module refuses to do, because each would be a loss dressed up as a convenience:

* **It never opens a local Zotero database.** Sync goes through `api.zotero.org` with a key that is
  sent only there. Scanning another program's SQLite files behind its back is not a connector.
* **It never reads attachments, annotations or notes.** The item request carries an explicit
  *inclusion* list of bibliographic types, so private note bodies are not transferred at all — not
  fetched and discarded. Anything the sync therefore does not see is counted and reported, so a
  narrow request cannot pass silently for a complete one.
* **A remote deletion is a tombstone, not a cascade.** The link is marked `remote_deleted`; the
  paper, its notes, its resource audits and its evidence all stay. Zotero stopping to hold a record
  says something about Zotero, and nothing about work done here.

Identity is `(library_type, library_id, item_key)` plus the remote version. DOI and arXiv are used to
*associate* a remote item with a paper already in the library, never to replace that identity — so
the same work in two libraries keeps two mappings, and a paper with no DOI still syncs.

The cursor moves only inside the transaction that writes the links. An interrupted sync therefore
re-reads the same window instead of skipping the items it never committed.
"""
from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass, field
from typing import Literal

from fastapi import HTTPException

from .db import Database, encode
from .models import PaperInput, normalize_arxiv, now
from .providers import ProviderClient, ProviderError
from .scheduling import Governor, paginate

HOST = "api.zotero.org"
API_VERSION = "3"
LIBRARY_TYPES = ("user", "group")
LINK_STATES = ("linked", "remote_deleted")
CHANGE_KINDS = ("added", "updated", "remote_deleted", "unchanged", "skipped")
# 50 rather than Zotero's 100: a page of items carrying abstracts can approach the 2 MiB response
# budget the shared client enforces, and a page that aborts mid-read costs a retry rather than a
# smaller one.
PAGE_SIZE = 50
MAX_PAGES = 40
MAX_ITEMS_PER_SYNC = 2000
# Requested explicitly, so a top-level note or an attachment is never transferred in the first
# place. A type Zotero adds later is not on this list and will be reported as unaccounted for rather
# than silently ignored.
BIBLIOGRAPHIC_ITEM_TYPES = (
    "journalArticle", "conferencePaper", "preprint", "report", "thesis", "book", "bookSection",
    "editedBook", "manuscript", "patent", "computerProgram", "document", "encyclopediaArticle",
    "dictionaryEntry", "newspaperArticle", "magazineArticle", "blogPost", "letter", "interview",
    "map", "artwork", "audioRecording", "videoRecording", "tvBroadcast", "radioBroadcast", "podcast",
    "presentation", "statute", "bill", "hearing", "case", "forumPost", "instantMessage", "email",
)
# Fields a sync may write. Everything else on a paper — notes, topics, reading status — belongs to
# the reader, and no remote payload can reach it.
SOURCE_FIELDS = ("title", "authors", "year", "venue", "doi", "arxiv_id", "paper_url", "abstract",
                 "version_label", "zotero_item_key", "zotero_library_id", "zotero_library_type")
KEY_PATTERN = re.compile(r"^[A-Z0-9]{8}$")
ARXIV_IN_EXTRA = re.compile(r"arxiv[:\s]*([0-9]{4}\.[0-9]{4,5}(?:v\d+)?|[a-z-]+(?:\.[A-Z]{2})?/\d{7}(?:v\d+)?)",
                            re.IGNORECASE)
TITLE_KEY_MIN_CHARS = 16


class ZoteroError(RuntimeError):
    """A refusal a caller can act on. Never a traceback, and never a partial write."""


def _text(value, limit: int) -> str:
    return " ".join(str(value or "").split())[:limit]


def _year(value) -> int | None:
    match = re.search(r"\b(1[89][0-9]{2}|20[0-9]{2}|21[0-9]{2})\b", str(value or ""))
    return int(match.group(1)) if match else None


def library_prefix(library_type: str, library_id: str) -> str:
    """The API path segment for one library. Validated, because it is interpolated into a URL."""
    if library_type not in LIBRARY_TYPES:
        raise ZoteroError(f"库类型只能是 {' 或 '.join(LIBRARY_TYPES)}")
    identifier = str(library_id or "").strip()
    if not identifier.isdigit() or not 1 <= len(identifier) <= 20:
        # Zotero library IDs are numeric for both user and group libraries. Anything else is refused
        # here rather than being placed into a path, where it could name another library.
        raise ZoteroError("库标识必须是数字（用户 ID 或群组 ID）")
    return f"/{'users' if library_type == 'user' else 'groups'}/{identifier}"


@dataclass
class Connection:
    """One library the user named. The key is held in memory for the length of a sync, never stored."""

    library_type: Literal["user", "group"]
    library_id: str
    api_key: str = ""
    label: str = ""
    collections: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)

    @property
    def identity(self) -> tuple[str, str]:
        return self.library_type, str(self.library_id)

    @property
    def prefix(self) -> str:
        return library_prefix(self.library_type, self.library_id)

    def public(self) -> dict:
        """Everything about this connection except the key. Safe to store, log and return."""
        return {"library_type": self.library_type, "library_id": str(self.library_id),
                "label": self.label, "collections": list(self.collections), "tags": list(self.tags),
                "has_api_key": bool(self.api_key)}


class ZoteroClient:
    """A bounded, read-only client for one library. GET only; there is no write method here."""

    def __init__(self, connection: Connection, *, transport=None, governor: Governor | None = None,
                 max_requests: int = 60, seconds: float = 180):
        if not connection.api_key:
            raise ZoteroError("还没有 API key；没有凭据就不会假装同步过")
        self.connection = connection
        self.client = ProviderClient(transport, max_calls=max_requests, seconds=seconds,
                                     read_timeout=20, governor=governor)
        self.requests = 0

    def close(self):
        self.client.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def _headers(self) -> dict:
        # The key goes to api.zotero.org and nowhere else: the shared client is allowlisted by host,
        # and this is the only header set that carries it.
        return {"Zotero-API-Key": self.connection.api_key,
                "Zotero-API-Version": API_VERSION, "Accept": "application/json"}

    def _get(self, path: str, params: dict | None = None):
        self.requests += 1
        try:
            payload = self.client.json(f"https://{HOST}{path}", params, self._headers())
        except ProviderError as exc:
            raise ZoteroError(str(exc)) from exc
        return payload, dict(self.client.last_headers)

    def library_version(self) -> int:
        """The library's current version, from a request that transfers no item content."""
        payload, _ = self._get(self.connection.prefix, {"format": "versions", "limit": "1"})
        version = self.client.last_headers.get("last-modified-version", "")
        try:
            return int(version)
        except (TypeError, ValueError):
            raise ZoteroError("接口没有返回 Last-Modified-Version；本次不能确定同步位置") from None

    def changed_versions(self, since: int) -> dict[str, int]:
        """`{item_key: version}` for everything that changed after `since`. Keys and numbers only."""
        collected: dict[str, int] = {}

        def fetch(cursor: str):
            start = int(cursor or 0)
            payload, _ = self._get(self.connection.prefix + "/items",
                                   {"format": "versions", "since": str(since),
                                    "limit": str(PAGE_SIZE), "start": str(start)})
            if not isinstance(payload, dict):
                raise ZoteroError("版本列表格式无效")
            for key, value in payload.items():
                if KEY_PATTERN.match(str(key)) and isinstance(value, int):
                    collected[str(key)] = value
            # A full page means there is probably another; a short one means the window is exhausted.
            return list(payload), str(start + PAGE_SIZE) if len(payload) >= PAGE_SIZE else ""

        outcome = paginate(fetch, governor=self.client.governor, provider="zotero", host=HOST,
                           max_pages=MAX_PAGES, label="item versions")
        if outcome.stop_reason not in ("complete", "empty_page"):
            raise ZoteroError(f"版本列表没有读完（{outcome.stop_reason}）；"
                              "本次不同步，因为漏掉的变更会被当成没有变化")
        return collected

    def collections(self) -> list[dict]:
        """Collection names and keys. Names only: a collection's *contents* are items, and those are
        read by `items()`, under the same inclusion list."""
        payload, _ = self._get(self.connection.prefix + "/collections", {"limit": str(PAGE_SIZE)})
        if not isinstance(payload, list):
            raise ZoteroError("集合列表格式无效")
        rows = []
        for row in payload:
            if not isinstance(row, dict):
                continue
            data = row.get("data") if isinstance(row.get("data"), dict) else {}
            key = _text(data.get("key") or row.get("key"), 8)
            if key:
                rows.append({"key": key, "name": _text(data.get("name"), 200),
                             "version": int(row.get("version") or 0)})
        return rows

    def deleted_keys(self, since: int) -> list[str]:
        """Keys deleted after `since`. Zotero reports these separately, and reports only keys."""
        payload, _ = self._get(self.connection.prefix + "/deleted", {"since": str(since)})
        if not isinstance(payload, dict):
            raise ZoteroError("删除列表格式无效")
        keys = payload.get("items") or []
        return [str(key) for key in keys if KEY_PATTERN.match(str(key))]

    def items(self, since: int, *, collections: list[str] = (), tags: list[str] = ()) -> tuple[list, dict]:
        """Bibliographic items changed after `since`, and the counts needed to say what was skipped.

        The `itemType` filter is an *inclusion* list, so notes, attachments and annotations are never
        transferred. That makes the response narrower than the change list, and the difference is
        returned rather than lost.
        """
        received: list[dict] = []
        stops: list[str] = []
        # Total-Results is the server's own statement of how much there is. If the pages stop early
        # while that number is still unmet, the read was truncated — even when the last page was
        # empty and the pager therefore reported "complete".
        promised = [0]

        def fetch(cursor: str):
            start = int(cursor or 0)
            params: dict = {"format": "json", "since": str(since), "limit": str(PAGE_SIZE),
                            "start": str(start),
                            "itemType": "||".join(BIBLIOGRAPHIC_ITEM_TYPES)}
            if collections:
                params["collection"] = collections[0]
            if tags:
                params["tag"] = "::".join(tags[:5])
            payload, headers = self._get(self.connection.prefix + "/items", params)
            if not isinstance(payload, list):
                raise ZoteroError("条目列表格式无效")
            received.extend(payload)
            try:
                promised[0] = max(promised[0], int(headers.get("total-results", 0) or 0))
            except (TypeError, ValueError):
                pass
            more = bool(payload) and start + len(payload) < promised[0]
            return payload, str(start + PAGE_SIZE) if more else ""

        outcome = paginate(fetch, governor=self.client.governor, provider="zotero", host=HOST,
                           max_pages=MAX_PAGES, label="items")
        if outcome.stop_reason not in ("complete", "empty_page"):
            stops.append(outcome.stop_reason)
        if promised[0] > len(received):
            stops.append(f"total_results_unmet（声明 {promised[0]} 条，实际读到 {len(received)} 条）")
        if len(received) > MAX_ITEMS_PER_SYNC:
            raise ZoteroError(f"本次变更超过 {MAX_ITEMS_PER_SYNC} 条；请缩小集合范围后分次同步")
        return received, {"pages_fetched": outcome.pages_fetched, "requests_used": outcome.requests_used,
                          "stop_reason": outcome.stop_reason, "stops": stops}


@dataclass
class RemoteItem:
    """One bibliographic item, normalised. Nothing here came from a note, an attachment or a model."""

    key: str
    version: int
    title: str
    creators: list[str]
    date: str
    doi: str
    arxiv_id: str
    url: str
    venue: str
    item_type: str
    abstract: str
    tags: list[str]
    collections: list[str]

    def paper(self, *, existing: dict | None = None) -> PaperInput:
        """A paper record. `existing` supplies the fields a reader owns and a sync must not touch."""
        base = existing or {}
        values = {
            "title": self.title, "authors": self.creators, "year": _year(self.date),
            "venue": self.venue, "doi": self.doi, "arxiv_id": self.arxiv_id,
            "paper_url": self.url, "abstract": self.abstract,
            "version_label": _text(re.search(r"v\d+$", self.arxiv_id).group()
                                   if re.search(r"v\d+$", self.arxiv_id) else "", 100),
            "zotero_item_key": self.key,
        }
        merged = {name: base.get(name) for name in
                  ("notes", "topics", "status", "version_label", "zotero_library_id",
                   "zotero_library_type") if name in base}
        merged.update(values)
        merged["zotero_item_key"] = self.key
        return PaperInput(**{k: v for k, v in merged.items() if v is not None})


def _creators(entries) -> list[str]:
    names = []
    for entry in entries or []:
        if not isinstance(entry, dict):
            continue
        literal = _text(entry.get("name"), 160)
        if not literal:
            literal = _text(" ".join(filter(None, [entry.get("firstName"), entry.get("lastName")])), 160)
        if literal and literal not in names:
            names.append(literal)
    return names[:30]


def _venue(data: dict) -> str:
    for name in ("publicationTitle", "conferenceName", "repository", "publisher", "university",
                 "institutionLabel", "websiteTitle", "bookTitle"):
        value = _text(data.get(name), 200)
        if value:
            return value
    return ""


def _arxiv(data: dict) -> str:
    for source in (data.get("extra"), data.get("url"), data.get("DOI")):
        match = ARXIV_IN_EXTRA.search(str(source or ""))
        if match:
            try:
                return normalize_arxiv(match.group(1))
            except ValueError:
                continue
    return ""


def remote_item(raw: dict) -> RemoteItem | None:
    """Normalise one API item. Returns None for anything that is not a bibliographic record."""
    if not isinstance(raw, dict):
        return None
    key = _text(raw.get("key"), 8)
    data = raw.get("data") if isinstance(raw.get("data"), dict) else {}
    if not KEY_PATTERN.match(key) or data.get("itemType") in {"attachment", "note", "annotation"}:
        return None
    title = _text(data.get("title"), 600)
    if not title:
        return None
    try:
        version = int(raw.get("version", 0))
    except (TypeError, ValueError):
        version = 0
    url = _text(data.get("url"), 2000)
    return RemoteItem(key=key, version=version, title=title, creators=_creators(data.get("creators")),
                      date=_text(data.get("date"), 120),
                      doi=_text(data.get("DOI"), 300), arxiv_id=_arxiv(data),
                      url=url if url.startswith("http") else "", venue=_venue(data),
                      item_type=_text(data.get("itemType"), 60),
                      abstract=_text(data.get("abstractNote"), 8000),
                      tags=[_text((tag or {}).get("tag"), 120) for tag in data.get("tags") or []
                            if isinstance(tag, dict)][:40],
                      collections=[_text(c, 8) for c in data.get("collections") or []])


def title_key(title: str) -> str:
    text = "".join(character for character in (title or "").lower() if character.isalnum())
    return text if len(text) >= TITLE_KEY_MIN_CHARS else ""


# --------------------------------------------------------------------------- storage

ZOTERO_SCHEMA = """
CREATE TABLE IF NOT EXISTS zotero_schema_version (version INTEGER NOT NULL);
INSERT INTO zotero_schema_version SELECT 1 WHERE NOT EXISTS (SELECT 1 FROM zotero_schema_version);
CREATE TABLE IF NOT EXISTS zotero_links (
 library_type TEXT NOT NULL, library_id TEXT NOT NULL, item_key TEXT NOT NULL,
 paper_id TEXT NOT NULL REFERENCES papers(id) ON DELETE CASCADE,
 remote_version INTEGER NOT NULL, state TEXT NOT NULL DEFAULT 'linked',
 item_type TEXT NOT NULL DEFAULT '', synced_at TEXT NOT NULL,
 PRIMARY KEY(library_type, library_id, item_key)
);
CREATE INDEX IF NOT EXISTS zotero_links_paper ON zotero_links(paper_id);
CREATE TABLE IF NOT EXISTS zotero_cursors (
 library_type TEXT NOT NULL, library_id TEXT NOT NULL,
 committed_version INTEGER NOT NULL DEFAULT 0, planned_version INTEGER NOT NULL DEFAULT 0,
 label TEXT NOT NULL DEFAULT '', scope TEXT NOT NULL DEFAULT '{}', updated_at TEXT NOT NULL,
 PRIMARY KEY(library_type, library_id)
);
CREATE TABLE IF NOT EXISTS zotero_collections (
 library_type TEXT NOT NULL, library_id TEXT NOT NULL, key TEXT NOT NULL,
 name TEXT NOT NULL, version INTEGER NOT NULL DEFAULT 0,
 selected INTEGER NOT NULL DEFAULT 0,
 PRIMARY KEY(library_type, library_id, key)
);
CREATE TABLE IF NOT EXISTS zotero_sync_log (
 id INTEGER PRIMARY KEY AUTOINCREMENT, library_type TEXT NOT NULL, library_id TEXT NOT NULL,
 at TEXT NOT NULL, applied INTEGER NOT NULL, data TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS zotero_sync_log_library ON zotero_sync_log(library_type, library_id, id);
"""


class ZoteroStore:
    """Mappings, cursors and the sync log. Holds no key and no note content."""

    SCHEMA_VERSION = 1

    def __init__(self, db: Database):
        self.db = db
        with db.connect() as con:
            # Additive: these tables are new and independent of the paper schema, so creating them
            # *is* the migration. A database stamped by a later version is refused rather than
            # downgraded, and nothing here rewrites a paper row.
            con.executescript(ZOTERO_SCHEMA)
            version = con.execute("SELECT version FROM zotero_schema_version").fetchone()[0]
            if version > self.SCHEMA_VERSION:
                raise RuntimeError(f"Unsupported zotero schema: {version}; no destructive migration "
                                   "was performed")

    # --- cursor

    def cursor(self, connection: Connection) -> dict:
        kind, identifier = connection.identity
        with self.db.connect() as con:
            row = con.execute("SELECT * FROM zotero_cursors WHERE library_type=? AND library_id=?",
                              (kind, identifier)).fetchone()
        if row is None:
            return {"library_type": kind, "library_id": identifier, "committed_version": 0,
                    "planned_version": 0, "label": connection.label, "scope": {}, "updated_at": "",
                    "note": "还没有同步过；第一次同步会读取整个库的当前状态"}
        result = dict(row)
        result["scope"] = _loads(result.pop("scope", "{}"))
        return result

    def _set_cursor(self, con, connection: Connection, *, committed: int, planned: int,
                    scope: dict) -> None:
        kind, identifier = connection.identity
        con.execute("INSERT INTO zotero_cursors(library_type,library_id,committed_version,"
                    "planned_version,label,scope,updated_at) VALUES (?,?,?,?,?,?,?) "
                    "ON CONFLICT(library_type,library_id) DO UPDATE SET "
                    "committed_version=excluded.committed_version, "
                    "planned_version=excluded.planned_version, label=excluded.label, "
                    "scope=excluded.scope, updated_at=excluded.updated_at",
                    (kind, identifier, committed, planned, connection.label[:200],
                     encode(scope), now()))

    # --- links

    def links(self, *, paper_id: str = "") -> list[dict]:
        query = "SELECT * FROM zotero_links"
        args: tuple = ()
        if paper_id:
            query += " WHERE paper_id=?"
            args = (paper_id,)
        with self.db.connect() as con:
            return [dict(row) for row in con.execute(query + " ORDER BY synced_at DESC", args)]

    def link(self, connection: Connection, key: str) -> dict | None:
        kind, identifier = connection.identity
        with self.db.connect() as con:
            row = con.execute("SELECT * FROM zotero_links WHERE library_type=? AND library_id=? "
                              "AND item_key=?", (kind, identifier, key)).fetchone()
        return dict(row) if row else None

    def status(self, connection: Connection) -> dict:
        """What is mapped, and what a sync would start from. Contains no key and no note text."""
        kind, identifier = connection.identity
        with self.db.connect() as con:
            rows = con.execute("SELECT state, COUNT(*) FROM zotero_links WHERE library_type=? "
                               "AND library_id=? GROUP BY state", (kind, identifier)).fetchall()
            papers = con.execute("SELECT COUNT(DISTINCT paper_id) FROM zotero_links WHERE "
                                 "library_type=? AND library_id=?", (kind, identifier)).fetchone()[0]
            runs = con.execute("SELECT at, applied, data FROM zotero_sync_log WHERE library_type=? "
                               "AND library_id=? ORDER BY id DESC LIMIT 10",
                               (kind, identifier)).fetchall()
            collections = con.execute("SELECT key,name,version,selected FROM zotero_collections "
                                      "WHERE library_type=? AND library_id=? ORDER BY name",
                                      (kind, identifier)).fetchall()
        return {"connection": connection.public(), "cursor": self.cursor(connection),
                "links": {row[0]: row[1] for row in rows}, "distinct_papers": papers,
                "collections": [{"key": row[0], "name": row[1], "version": row[2],
                                 "selected": bool(row[3])} for row in collections],
                "recent_syncs": [{"at": row[0], "applied": bool(row[1]), **_loads(row[2])}
                                 for row in runs]}

    def save_collections(self, connection: Connection, rows: list[dict],
                         selected: list[str] | None = None) -> int:
        kind, identifier = connection.identity
        keep = set(selected) if selected is not None else None
        with self.db.connect() as con:
            previous = {row[0]: row[1] for row in con.execute(
                "SELECT key, selected FROM zotero_collections WHERE library_type=? AND library_id=?",
                (kind, identifier))}
            for row in rows:
                key = _text(row.get("key"), 8)
                if not key:
                    continue
                chosen = bool(previous.get(key, 0)) if keep is None else key in keep
                con.execute("INSERT INTO zotero_collections(library_type,library_id,key,name,version,"
                            "selected) VALUES (?,?,?,?,?,?) "
                            "ON CONFLICT(library_type,library_id,key) DO UPDATE SET "
                            "name=excluded.name, version=excluded.version, selected=excluded.selected",
                            (kind, identifier, key, _text(row.get("name"), 200),
                             int(row.get("version") or 0), int(chosen)))
        return len(rows)

    def select_collections(self, connection: Connection, keys: list[str]) -> dict:
        """Choosing which collections to sync is a scope decision, so it is explicit and recorded."""
        kind, identifier = connection.identity
        with self.db.connect() as con:
            known = {row[0] for row in con.execute(
                "SELECT key FROM zotero_collections WHERE library_type=? AND library_id=?",
                (kind, identifier))}
            unknown = [key for key in keys if key not in known]
            if unknown:
                raise HTTPException(404, f"这些集合不属于该库：{'、'.join(unknown[:8])}")
            con.execute("UPDATE zotero_collections SET selected=0 WHERE library_type=? AND library_id=?",
                        (kind, identifier))
            for key in keys:
                con.execute("UPDATE zotero_collections SET selected=1 WHERE library_type=? AND "
                            "library_id=? AND key=?", (kind, identifier, key))
        return {"selected": list(keys), "note": "只有选中的集合会被同步；取消选择不会删除已经读入的条目"}

    # --- sync write

    def disconnect(self, connection: Connection, *, remove_links: bool = False) -> dict:
        """Forget the connection. Never touches papers, notes, resources or evidence.

        `remove_links` drops the mapping rows only, which is what "delete the remote mapping" means:
        the research record on this side is independent of whether Zotero still holds the item.
        """
        kind, identifier = connection.identity
        with self.db.connect() as con:
            links = con.execute("DELETE FROM zotero_links WHERE library_type=? AND library_id=?",
                                (kind, identifier)).rowcount if remove_links else 0
            con.execute("DELETE FROM zotero_cursors WHERE library_type=? AND library_id=?",
                        (kind, identifier))
            con.execute("DELETE FROM zotero_collections WHERE library_type=? AND library_id=?",
                        (kind, identifier))
            papers = con.execute("SELECT COUNT(*) FROM papers").fetchone()[0]
        return {"disconnected": True, "removed_links": links, "papers_remaining": papers,
                "note": "断开连接不会删除文献库里的任何论文、笔记、资源或证据；"
                        "删除的只是同步游标、集合选择" + ("和远端映射" if remove_links else "")}


def _loads(value, default=None):
    import json
    try:
        parsed = json.loads(value or "{}")
    except ValueError:
        return default if default is not None else {}
    return parsed if isinstance(parsed, (dict, list)) else (default if default is not None else {})
