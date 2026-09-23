"""Versioned, additive task storage in the same database as the literature library.

Schema v2 adds the conversation layer: a run belongs to a conversation, carries a turn number and a
kind, and stores an immutable `origin` snapshot of what authorized it. Schema v3 adds `owner`, so
a run, a conversation and a task default belong to whoever created them. Both migrations are
additive — columns are added with defaults, legacy runs each become a one-turn conversation owned
by `local`, and no row is rewritten or dropped.

Methods that an HTTP endpoint calls take a required `owner`; methods that only the runtime worker
calls are keyed by run id and are reachable *after* an ownership check. That split is the whole
authorization model here, so it is stated rather than implied: a route that calls an unscoped
helper directly is a route that serves somebody else's task.
"""
from __future__ import annotations

import hashlib
import json
import re
from uuid import uuid4

from fastapi import HTTPException

from ..db import Database, encode
from ..knowledge import append_source_snapshot, ensure_paper_version, metadata_snapshot
from ..models import PaperInput, now
from .schemas import SessionCaps

SCHEMA_TARGET = 3
AGENT_SCHEMA = """
CREATE TABLE IF NOT EXISTS agent_schema_version (version INTEGER NOT NULL);
INSERT INTO agent_schema_version SELECT 3 WHERE NOT EXISTS (SELECT 1 FROM agent_schema_version);
CREATE TABLE IF NOT EXISTS agent_runs (
 id TEXT PRIMARY KEY, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
 status TEXT NOT NULL, goal TEXT NOT NULL, config TEXT NOT NULL, params TEXT NOT NULL,
 state TEXT NOT NULL, error TEXT NOT NULL DEFAULT '', cancel_requested INTEGER NOT NULL DEFAULT 0,
 conversation_id TEXT NOT NULL DEFAULT '', turn INTEGER NOT NULL DEFAULT 1,
 kind TEXT NOT NULL DEFAULT 'new', origin TEXT NOT NULL DEFAULT '',
 idempotency_key TEXT NOT NULL DEFAULT '', owner TEXT NOT NULL DEFAULT 'local'
);
CREATE TABLE IF NOT EXISTS agent_conversations (
 id TEXT PRIMARY KEY, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
 goal TEXT NOT NULL, caps TEXT NOT NULL, note TEXT NOT NULL DEFAULT '',
 owner TEXT NOT NULL DEFAULT 'local'
);
CREATE TABLE IF NOT EXISTS agent_events (
 id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL REFERENCES agent_runs(id),
 at TEXT NOT NULL, kind TEXT NOT NULL, data TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS agent_events_run ON agent_events(run_id, id);
CREATE TABLE IF NOT EXISTS agent_evidence (
 id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES agent_runs(id),
 tool_call_id TEXT NOT NULL, data TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS agent_evidence_run ON agent_evidence(run_id);
CREATE TABLE IF NOT EXISTS agent_tool_results (
 run_id TEXT NOT NULL REFERENCES agent_runs(id), call_id TEXT NOT NULL, data TEXT NOT NULL,
 PRIMARY KEY(run_id, call_id)
);
CREATE TABLE IF NOT EXISTS agent_imports (
 run_id TEXT NOT NULL REFERENCES agent_runs(id), evidence_id TEXT NOT NULL,
 paper_id TEXT NOT NULL, at TEXT NOT NULL, PRIMARY KEY(run_id, evidence_id)
);
CREATE TABLE IF NOT EXISTS agent_settings (
 owner TEXT NOT NULL DEFAULT 'local', key TEXT NOT NULL, value TEXT NOT NULL,
 PRIMARY KEY(owner, key)
);
"""
# Split out from AGENT_SCHEMA on purpose: both indexes name columns that only exist from v2 onward,
# so on a v1 database they have to be created *after* the migration adds those columns. Running them
# as part of the same script would fail on exactly the database that needs migrating.
AGENT_INDEXES_V2 = """
CREATE INDEX IF NOT EXISTS agent_runs_conversation ON agent_runs(conversation_id, turn);
CREATE UNIQUE INDEX IF NOT EXISTS agent_runs_idempotency
 ON agent_runs(conversation_id, idempotency_key) WHERE idempotency_key != '';
CREATE INDEX IF NOT EXISTS agent_runs_owner ON agent_runs(owner, created_at DESC);
CREATE INDEX IF NOT EXISTS agent_conversations_owner ON agent_conversations(owner, updated_at DESC);
"""
# Columns v2 adds to a v1 `agent_runs`. Applied one at a time, only where missing, so re-running the
# migration is a no-op and a hand-edited database is not clobbered.
RUN_COLUMNS_V2 = {
    "conversation_id": "TEXT NOT NULL DEFAULT ''",
    "turn": "INTEGER NOT NULL DEFAULT 1",
    "kind": "TEXT NOT NULL DEFAULT 'new'",
    "origin": "TEXT NOT NULL DEFAULT ''",
    "idempotency_key": "TEXT NOT NULL DEFAULT ''",
}
MIGRATION_NOTE = ("由 v1 迁移生成：这个既有任务各自成为一个会话，累计账本按其最近检查点填写，"
                  "没有重置任何计数。")


def default_caps() -> dict:
    return SessionCaps().model_dump()


class TaskStore:
    def __init__(self, db: Database):
        self.db = db
        with db.connect() as con:
            # Existing paper schema remains v1. Agent tables have their own version.
            #
            # The DDL runs first and the migration second: on a fresh database the script already
            # creates every v2 column and stamps version 2, while on a v1 database `CREATE TABLE IF
            # NOT EXISTS` leaves the old `agent_runs` alone and only adds what is missing
            # (`agent_conversations`, the indexes), which is exactly what the migration then needs.
            con.executescript(AGENT_SCHEMA)
            version = con.execute("SELECT version FROM agent_schema_version").fetchone()[0]
            if version > SCHEMA_TARGET:
                raise RuntimeError("Unsupported agent schema; no destructive migration was performed")
            if version < 2:
                self._migrate_to_v2(con)
            if version < SCHEMA_TARGET:
                self._migrate_to_v3(con)
            con.executescript(AGENT_INDEXES_V2)
            if con.execute("SELECT version FROM agent_schema_version").fetchone()[0] != SCHEMA_TARGET:
                raise RuntimeError("Unsupported agent schema; no destructive migration was performed")

    def _migrate_to_v2(self, con):
        """v1 -> v2: add the conversation columns, then give every orphan run a conversation.

        Nothing is deleted and no existing value is overwritten except the three columns that did
        not exist before. A run that already has a conversation_id is left alone, so a migration
        interrupted halfway finishes the rest on the next start instead of redoing it.
        """
        have = {row[1] for row in con.execute("PRAGMA table_info(agent_runs)")}
        for name, declaration in RUN_COLUMNS_V2.items():
            if name not in have:
                con.execute(f"ALTER TABLE agent_runs ADD COLUMN {name} {declaration}")
        orphans = con.execute("SELECT id,goal,created_at,updated_at,state FROM agent_runs "
                              "WHERE conversation_id='' ORDER BY created_at").fetchall()
        for row in orphans:
            state = json.loads(row["state"])
            cid = "cv_" + uuid4().hex[:16]
            con.execute("INSERT INTO agent_conversations(id,created_at,updated_at,goal,caps,note) "
                        "VALUES (?,?,?,?,?,?)",
                        (cid, row["created_at"], row["updated_at"], row["goal"],
                         encode(default_caps()), MIGRATION_NOTE))
            con.execute("UPDATE agent_runs SET conversation_id=?, turn=1, kind='new' WHERE id=?",
                        (cid, row["id"]))
            con.execute("INSERT INTO agent_events(run_id,at,kind,data) VALUES (?,?,?,?)",
                        (row["id"], now(), "conversation_adopted",
                         encode({"conversation_id": cid, "message": MIGRATION_NOTE,
                                 "checkpoint_model_calls": state.get("model_calls", 0),
                                 "checkpoint_tool_calls": state.get("tool_calls", 0)})))
        con.execute("UPDATE agent_schema_version SET version=?", (2,))

    def _migrate_to_v3(self, con):
        """v2 -> v3: every existing row belongs to the single user a local database had.

        Naming that owner `local` is what keeps the history away from an account created later: a
        new reader logs in to an empty task list, not to somebody else's runs, budgets and
        defaults. `agent_settings` has to be rebuilt because its primary key gains a column.
        """
        for table in ("agent_runs", "agent_conversations"):
            if "owner" not in {row[1] for row in con.execute(f"PRAGMA table_info({table})")}:
                con.execute(f"ALTER TABLE {table} ADD COLUMN owner TEXT NOT NULL DEFAULT 'local'")
        settings = {row[1] for row in con.execute("PRAGMA table_info(agent_settings)")}
        if "owner" not in settings:
            con.execute("CREATE TABLE agent_settings_v3 (owner TEXT NOT NULL DEFAULT 'local', "
                        "key TEXT NOT NULL, value TEXT NOT NULL, PRIMARY KEY(owner, key))")
            moved = con.execute("INSERT INTO agent_settings_v3(owner,key,value) "
                                "SELECT 'local',key,value FROM agent_settings").rowcount
            expected = con.execute("SELECT COUNT(*) FROM agent_settings").fetchone()[0]
            if moved != expected:
                raise RuntimeError(f"agent_settings 迁移数量不符（{moved} != {expected}）；"
                                   "未删除原表")
            con.execute("DROP TABLE agent_settings")
            con.execute("ALTER TABLE agent_settings_v3 RENAME TO agent_settings")
        con.execute("UPDATE agent_schema_version SET version=?", (SCHEMA_TARGET,))

    def start_conversation(self, goal: str, caps: dict | None = None, note: str = "",
                           *, owner: str) -> str:
        cid, stamp = "cv_" + uuid4().hex[:16], now()
        with self.db.connect() as con:
            con.execute("INSERT INTO agent_conversations(id,created_at,updated_at,goal,caps,note,"
                        "owner) VALUES (?,?,?,?,?,?,?)",
                        (cid, stamp, stamp, goal[:6000], encode(caps or default_caps()), note,
                         owner))
        return cid

    def conversation(self, cid: str, *, owner: str) -> dict:
        with self.db.connect() as con:
            row = con.execute("SELECT * FROM agent_conversations WHERE id=? AND owner=?",
                              (cid, owner)).fetchone()
        if row is None:
            # 404, not 403: the difference would confirm that the id belongs to somebody.
            raise HTTPException(404, "会话不存在")
        result = dict(row)
        result.pop("owner", None)
        result["caps"] = json.loads(result["caps"])
        result["ledger"] = self.ledger(cid, owner=owner)
        result["runs"] = self.runs_in(cid, owner=owner)
        return result

    def conversations(self, *, owner: str) -> list:
        with self.db.connect() as con:
            rows = [dict(row) for row in con.execute(
                "SELECT id,created_at,updated_at,goal,note FROM agent_conversations WHERE owner=? "
                "ORDER BY updated_at DESC LIMIT 100", (owner,))]
        for row in rows:
            row["ledger"] = self.ledger(row["id"], owner=owner)
        return rows

    def runs_in(self, cid: str, *, owner: str = "") -> list:
        """Turns of one conversation, oldest first, with the counters that feed the ledger.

        `owner` narrows the read when the caller has one; the conversation itself is already
        owner-checked by `conversation()`, so the parameter is a second gate, not the only one.
        """
        query = ("SELECT id,turn,kind,status,goal,created_at,updated_at,error,state "
                 "FROM agent_runs WHERE conversation_id=?")
        args: tuple = (cid,)
        if owner:
            query += " AND owner=?"
            args += (owner,)
        with self.db.connect() as con:
            rows = con.execute(query + " ORDER BY turn, created_at", args).fetchall()
        out = []
        for row in rows:
            state = json.loads(row["state"])
            out.append({"id": row["id"], "turn": row["turn"], "kind": row["kind"],
                        "status": row["status"], "goal": row["goal"], "error": row["error"],
                        "created_at": row["created_at"], "updated_at": row["updated_at"],
                        "model_calls": state.get("model_calls", 0),
                        "tool_calls": state.get("tool_calls", 0),
                        "unreported_calls": (state.get("usage") or {}).get("unreported_calls", 0),
                        "has_report": bool(state.get("report"))})
        return out

    def ledger(self, cid: str, *, owner: str = "") -> dict:
        """Cumulative across every turn, recomputed from the runs rather than cached.

        Recomputing is the point: a counter that is incremented somewhere can also be reset
        somewhere, and the whole guarantee this ledger exists for is that it cannot be. A follow-up
        therefore cannot escape a cap by being a new run.
        """
        model_calls = tool_calls = unreported = turns = 0
        for run in self.runs_in(cid, owner=owner):
            model_calls += run["model_calls"]
            tool_calls += run["tool_calls"]
            unreported += run["unreported_calls"]
            turns += 1
        return {"model_calls": model_calls, "tool_calls": tool_calls, "turns": turns,
                # Honest about what we cannot know: a call whose provider did not report usage may
                # still have been billed, and an in-flight request may be billed after a cancel.
                "unreported_calls": unreported,
                "note": "累计值由本会话每一轮的检查点重新求和，不会被新一轮重置；"
                        "unreported_calls 是提供商未回报用量的调用数，可能已计费。"}

    def next_turn(self, cid: str, *, owner: str = "") -> int:
        query = "SELECT COALESCE(MAX(turn),0) FROM agent_runs WHERE conversation_id=?"
        args: tuple = (cid,)
        if owner:
            query += " AND owner=?"
            args += (owner,)
        with self.db.connect() as con:
            row = con.execute(query, args).fetchone()
        return int(row[0]) + 1

    def run_by_key(self, cid: str, key: str, *, owner: str = "") -> str | None:
        if not key:
            return None
        query = "SELECT id FROM agent_runs WHERE conversation_id=? AND idempotency_key=?"
        args: tuple = (cid, key)
        if owner:
            query += " AND owner=?"
            args += (owner,)
        with self.db.connect() as con:
            row = con.execute(query, args).fetchone()
        return row[0] if row else None

    def raise_caps(self, cid: str, caps: dict, *, owner: str, reason: str, run_id: str):
        with self.db.connect() as con:
            con.execute("UPDATE agent_conversations SET caps=?, updated_at=? WHERE id=? AND owner=?",
                        (encode(caps), now(), cid, owner))
        self.event(run_id, "session_caps_raised",
                   {"conversation_id": cid, "caps": caps, "reason": reason,
                    "message": "会话上限被显式提高；累计账本没有重置"})

    def origin(self, rid: str, *, owner: str) -> dict:
        """The immutable snapshot of what authorized this turn. Written once, never updated."""
        with self.db.connect() as con:
            row = con.execute("SELECT origin FROM agent_runs WHERE id=? AND owner=?",
                              (rid, owner)).fetchone()
        if row is None:
            raise HTTPException(404, "研究任务不存在")
        try:
            stored = json.loads(row[0] or "{}")
        except ValueError:
            return {}
        return stored if isinstance(stored, dict) else {}

    def create(self, params: dict, config: dict, state: dict, *, owner: str,
               conversation_id: str = "", turn: int = 1, kind: str = "new",
               origin: dict | None = None, idempotency_key: str = "") -> str:
        rid, stamp = str(uuid4()), now()
        if not conversation_id:
            conversation_id = self.start_conversation(params["goal"], owner=owner)
        with self.db.connect() as con:
            con.execute("INSERT INTO agent_runs(id,created_at,updated_at,status,goal,config,params,"
                        "state,conversation_id,turn,kind,origin,idempotency_key,owner) "
                        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (rid, stamp, stamp, "queued", params["goal"], encode(config), encode(params),
                         encode(state), conversation_id, turn, kind, encode(origin or {}),
                         idempotency_key, owner))
            con.execute("UPDATE agent_conversations SET updated_at=? WHERE id=?", (stamp, conversation_id))
        self.event(rid, "created", {"conversation_id": conversation_id, "turn": turn, "kind": kind,
                                    "message": "任务已创建；只有用户确认后才会把候选论文写入文献库"})
        return rid

    def get(self, rid: str, *, owner: str = "", internal=False) -> dict:
        """One run. `owner` is required for anything a request can reach; the worker, which already
        holds the run it is executing, reads it by id with `internal=True`.

        The check is a hard error rather than a widening of the query, because the failure it prevents
        is invisible: an unscoped read returns a row that looks exactly like an authorized one.
        """
        if not internal and not owner:
            raise ValueError("a request-facing read of a run must name its owner")
        query = "SELECT * FROM agent_runs WHERE id=?"
        args: tuple = (rid,)
        if owner:
            query += " AND owner=?"
            args += (owner,)
        with self.db.connect() as con:
            row = con.execute(query, args).fetchone()
        if row is None:
            raise HTTPException(404, "研究任务不存在")
        result = dict(row)
        if not internal:
            # Not part of the public shape: whoever can read this row already knows whose it is, and
            # an owner id in a response is an id somebody can collect and compare.
            result.pop("owner", None)
        for key in ("config", "params", "state"):
            result[key] = json.loads(result[key])
        result["origin"] = json.loads(result.get("origin") or "{}")
        if not internal:
            state = result.pop("state")
            result.update({k: state.get(k) for k in ("plan", "report", "model_calls", "tool_calls",
                                                     "usage", "resumes", "report_delta")})
            result["evidence"] = self.evidence(rid)
        return result

    def list(self, *, owner: str) -> list:
        with self.db.connect() as con:
            return [dict(row) for row in con.execute(
                "SELECT id,created_at,updated_at,status,goal,error,conversation_id,turn,kind "
                "FROM agent_runs WHERE owner=? ORDER BY created_at DESC LIMIT 100", (owner,))]

    def setting(self, key: str, *, owner: str) -> dict:
        """One owner's setting row. Missing or unreadable values return {}.

        Defaults are per owner rather than global: a task budget one reader chose must not become
        the budget somebody else's task runs under.
        """
        with self.db.connect() as con:
            row = con.execute("SELECT value FROM agent_settings WHERE owner=? AND key=?",
                              (owner, key)).fetchone()
        if row is None:
            return {}
        try:
            stored = json.loads(row[0])
        except ValueError:
            return {}
        return stored if isinstance(stored, dict) else {}

    def save_setting(self, key: str, value: dict, *, owner: str) -> dict:
        with self.db.connect() as con:
            con.execute("INSERT OR REPLACE INTO agent_settings(owner,key,value) VALUES(?,?,?)",
                        (owner, key, encode(value)))
        return value

    def checkpoint(self, rid, state, status=None, error=""):
        with self.db.connect() as con:
            if status is None:
                con.execute("UPDATE agent_runs SET state=?,updated_at=?,error=? WHERE id=?", (encode(state), now(), error, rid))
            else:
                con.execute("UPDATE agent_runs SET state=?,status=?,updated_at=?,error=? WHERE id=?", (encode(state), status, now(), error, rid))

    def event(self, rid, kind, data):
        with self.db.connect() as con:
            con.execute("INSERT INTO agent_events(run_id,at,kind,data) VALUES (?,?,?,?)", (rid, now(), kind, encode(data)))

    def events(self, rid, after=0, *, owner: str = ""):
        self.get(rid, owner=owner, internal=True)
        with self.db.connect() as con:
            return [{**dict(row), "data": json.loads(row["data"])} for row in con.execute(
                "SELECT id,at,kind,data FROM agent_events WHERE run_id=? AND id>? ORDER BY id LIMIT 200", (rid, after))]

    def evidence(self, rid):
        with self.db.connect() as con:
            rows = con.execute("SELECT id,data FROM agent_evidence WHERE run_id=? ORDER BY rowid", (rid,))
            return [{"id": row["id"], **json.loads(row["data"])} for row in rows]

    def evidence_owner(self, eid: str) -> dict | None:
        """Which run, which conversation and which owner an evidence id belongs to.

        Reuse is scoped by this: an id from another conversation is refused with a message that says
        so, rather than being treated as "not found" (which would invite retrying it) or, worse,
        being honoured. The owner is returned for the same reason — an id that happens to be valid
        in somebody else's task is not this caller's material, and the conversation check alone
        would not say so if a conversation id were ever guessed or reused.
        """
        with self.db.connect() as con:
            row = con.execute("SELECT e.run_id, e.data, r.conversation_id, r.turn, r.status, "
                              "r.owner FROM agent_evidence e JOIN agent_runs r ON r.id = e.run_id "
                              "WHERE e.id=?", (eid,)).fetchone()
        if row is None:
            return None
        return {"run_id": row["run_id"], "conversation_id": row["conversation_id"],
                "turn": row["turn"], "status": row["status"], "owner": row["owner"],
                "data": json.loads(row["data"])}

    def seed_evidence(self, rid: str, rows: list[dict]) -> list[str]:
        """Carry reused material into a new turn as evidence rows of that turn.

        Ids are derived from (run, origin) rather than random, so seeding twice — a crash between
        the insert and the checkpoint, or a double submit — produces the same rows and INSERT OR
        IGNORE makes the second pass a no-op. The original body, retrieval time and parent turn
        travel with the row; nothing is re-fetched here.
        """
        written = []
        with self.db.connect() as con:
            con.execute("BEGIN IMMEDIATE")
            for index, row in enumerate(rows):
                origin_key = json.dumps(row.get("reused_from") or index, ensure_ascii=False, sort_keys=True)
                eid = "ev_" + hashlib.sha256(f"{rid}|{origin_key}".encode("utf-8")).hexdigest()[:16]
                data = {**row["data"], "id": eid}
                con.execute("INSERT OR IGNORE INTO agent_evidence VALUES (?,?,?,?)",
                            (eid, rid, f"reuse_{index}", encode(data)))
                written.append(eid)
        return written

    def previous_report(self, rid: str) -> dict | None:
        """The latest earlier turn of the same conversation that produced a report.

        Used to say what changed, never to overwrite anything: the earlier report stays readable and
        exportable under its own run id.
        """
        with self.db.connect() as con:
            row = con.execute("SELECT conversation_id, turn FROM agent_runs WHERE id=?", (rid,)).fetchone()
            if row is None:
                return None
            candidates = con.execute(
                "SELECT id, turn, state FROM agent_runs WHERE conversation_id=? AND turn<? "
                "ORDER BY turn DESC", (row["conversation_id"], row["turn"])).fetchall()
        for candidate in candidates:
            state = json.loads(candidate["state"])
            if state.get("report"):
                return {"run_id": candidate["id"], "turn": candidate["turn"], "report": state["report"]}
        return None

    def cached_tool(self, rid, cid):
        with self.db.connect() as con:
            row = con.execute("SELECT data FROM agent_tool_results WHERE run_id=? AND call_id=?", (rid, cid)).fetchone()
        return json.loads(row[0]) if row else None

    def save_tool(self, rid, cid, tool, payload):
        """Commit evidence and replayable tool result atomically, before the next model call."""
        with self.db.connect() as con:
            con.execute("BEGIN IMMEDIATE")
            row = con.execute("SELECT data FROM agent_tool_results WHERE run_id=? AND call_id=?", (rid, cid)).fetchone()
            if row:
                return json.loads(row[0])
            result = {k: v for k, v in payload.items() if k != "documents"}
            result["evidence"] = []
            for doc in payload.get("documents", [])[:12]:
                eid = "ev_" + uuid4().hex[:16]
                evidence = {**doc, "tool": tool, "retrieved_at": now()}
                con.execute("INSERT INTO agent_evidence VALUES (?,?,?,?)", (eid, rid, cid, encode(evidence)))
                result["evidence"].append({"id": eid, **evidence})
            con.execute("INSERT INTO agent_tool_results VALUES (?,?,?)", (rid, cid, encode(result)))
            return result

    def request_cancel(self, rid, *, owner: str):
        with self.db.connect() as con:
            changed = con.execute("UPDATE agent_runs SET cancel_requested=1 WHERE id=? AND owner=? "
                                  "AND status IN ('queued','running')", (rid, owner)).rowcount
        if not changed:
            raise HTTPException(409, "任务已停止，或当前不在运行")
        self.event(rid, "cancel_requested", {"message": "已请求停止；当前网络请求结束或超时后生效"})

    def cancelled(self, rid):
        with self.db.connect() as con:
            return bool(con.execute("SELECT cancel_requested FROM agent_runs WHERE id=?", (rid,)).fetchone()[0])

    def recover(self):
        with self.db.connect() as con:
            ids = [row[0] for row in con.execute("SELECT id FROM agent_runs WHERE status IN ('queued','running')")]
            con.execute("UPDATE agent_runs SET status='interrupted',error=?,updated_at=? WHERE status IN ('queued','running')",
                        ("进程已重启；任务没有自动重发。配置同一模型后可手动恢复", now()))
        for rid in ids:
            self.event(rid, "interrupted", {"message": "任务进程中断；已保留最近检查点和证据"})

    def reset_cancel(self, rid):
        with self.db.connect() as con:
            con.execute("UPDATE agent_runs SET cancel_requested=0 WHERE id=?", (rid,))

    def import_paper(self, rid, eid, *, owner: str):
        """Only source-derived metadata is importable; the model cannot supply arbitrary writes."""
        with self.db.connect() as con:
            con.execute("BEGIN IMMEDIATE")
            record = con.execute("SELECT e.data FROM agent_evidence e JOIN agent_runs r ON r.id=e.run_id "
                                 "WHERE e.run_id=? AND e.id=? AND r.owner=?",
                                 (rid, eid, owner)).fetchone()
            if not record:
                raise HTTPException(404, "当前任务中不存在这条证据")
            source = json.loads(record[0])
            if not source.get("paper") or source.get("kind") != "paper":
                raise HTTPException(422, "此证据不是可导入的论文元数据")
            paper = PaperInput.model_validate(source["paper"])
            imported = con.execute("SELECT paper_id FROM agent_imports WHERE run_id=? AND evidence_id=?", (rid, eid)).fetchone()
            if imported and con.execute("SELECT id FROM papers WHERE id=? AND owner=?",
                                        (imported[0], owner)).fetchone():
                return {"paper_id": imported[0], "created": False}
            arxiv = re.sub(r"v\d+$", "", paper.arxiv_id)
            existing = con.execute("SELECT id FROM papers WHERE owner=? AND ((doi != '' AND doi=?) "
                                   "OR (arxiv_base != '' AND arxiv_base=?))",
                                   (owner, paper.doi, arxiv)).fetchone()
            if existing:
                pid, created = existing[0], False
            else:
                pid, stamp, created = str(uuid4()), now(), True
                con.execute("INSERT INTO papers(id,data,doi,arxiv_base,created_at,updated_at,"
                            "is_demo,owner) VALUES (?,?,?,?,?,?,0,?)",
                            (pid, encode(paper.model_dump(mode="json")), paper.doi, arxiv, stamp,
                             stamp, owner))
            paper_data = paper.model_dump(mode="json")
            version_id = ensure_paper_version(con, pid, owner, paper_data,
                                              origin="agent_approved_import", created_at=now(),
                                              is_current=created)
            append_source_snapshot(con, work_id=pid, owner=owner,
                                   paper_version_id=version_id, kind="agent_approved_source",
                                   payload={"paper": metadata_snapshot(paper_data),
                                            "locator": source.get("locator", "")},
                                   retrieved_at=now(), source_url=source.get("source_url", ""),
                                   locator=source.get("locator", ""))
            con.execute("INSERT OR REPLACE INTO agent_imports VALUES (?,?,?,?)", (rid, eid, pid, now()))
            con.execute("INSERT INTO agent_events(run_id,at,kind,data) VALUES (?,?,?,?)", (rid, now(), "approved_import", encode({"evidence_id": eid, "paper_id": pid, "created": created})))
        return {"paper_id": pid, "created": created}
