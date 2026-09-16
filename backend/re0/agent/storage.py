"""Versioned, additive task storage in the same database as the literature library."""
from __future__ import annotations

import json
import re
from uuid import uuid4

from fastapi import HTTPException

from ..db import Database, encode
from ..models import PaperInput, now

AGENT_SCHEMA = """
CREATE TABLE IF NOT EXISTS agent_schema_version (version INTEGER NOT NULL);
INSERT INTO agent_schema_version SELECT 1 WHERE NOT EXISTS (SELECT 1 FROM agent_schema_version);
CREATE TABLE IF NOT EXISTS agent_runs (
 id TEXT PRIMARY KEY, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
 status TEXT NOT NULL, goal TEXT NOT NULL, config TEXT NOT NULL, params TEXT NOT NULL,
 state TEXT NOT NULL, error TEXT NOT NULL DEFAULT '', cancel_requested INTEGER NOT NULL DEFAULT 0
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
 key TEXT PRIMARY KEY, value TEXT NOT NULL
);
"""


class TaskStore:
    def __init__(self, db: Database):
        self.db = db
        with db.connect() as con:
            # Existing paper schema remains v1. Agent tables have their own version.
            con.executescript(AGENT_SCHEMA)
            if con.execute("SELECT version FROM agent_schema_version").fetchone()[0] != 1:
                raise RuntimeError("Unsupported agent schema; no destructive migration was performed")

    def create(self, params: dict, config: dict, state: dict) -> str:
        rid, stamp = str(uuid4()), now()
        with self.db.connect() as con:
            con.execute("INSERT INTO agent_runs(id,created_at,updated_at,status,goal,config,params,state) VALUES (?,?,?,?,?,?,?,?)",
                        (rid, stamp, stamp, "queued", params["goal"], encode(config), encode(params), encode(state)))
        self.event(rid, "created", {"message": "任务已创建；只有用户确认后才会把候选论文写入文献库"})
        return rid

    def get(self, rid: str, *, internal=False) -> dict:
        with self.db.connect() as con:
            row = con.execute("SELECT * FROM agent_runs WHERE id=?", (rid,)).fetchone()
        if row is None:
            raise HTTPException(404, "研究任务不存在")
        result = dict(row)
        for key in ("config", "params", "state"):
            result[key] = json.loads(result[key])
        if not internal:
            state = result.pop("state")
            result.update({k: state.get(k) for k in ("plan", "report", "model_calls", "tool_calls", "usage", "resumes")})
            result["evidence"] = self.evidence(rid)
        return result

    def list(self) -> list:
        with self.db.connect() as con:
            return [dict(row) for row in con.execute("SELECT id,created_at,updated_at,status,goal,error FROM agent_runs ORDER BY created_at DESC LIMIT 100")]

    def setting(self, key: str) -> dict:
        """Workspace-level setting row. Missing or unreadable values return {}."""
        with self.db.connect() as con:
            row = con.execute("SELECT value FROM agent_settings WHERE key=?", (key,)).fetchone()
        if row is None:
            return {}
        try:
            stored = json.loads(row[0])
        except ValueError:
            return {}
        return stored if isinstance(stored, dict) else {}

    def save_setting(self, key: str, value: dict) -> dict:
        with self.db.connect() as con:
            con.execute("INSERT OR REPLACE INTO agent_settings(key,value) VALUES(?,?)", (key, encode(value)))
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

    def events(self, rid, after=0):
        self.get(rid, internal=True)
        with self.db.connect() as con:
            return [{**dict(row), "data": json.loads(row["data"])} for row in con.execute(
                "SELECT id,at,kind,data FROM agent_events WHERE run_id=? AND id>? ORDER BY id LIMIT 200", (rid, after))]

    def evidence(self, rid):
        with self.db.connect() as con:
            rows = con.execute("SELECT id,data FROM agent_evidence WHERE run_id=? ORDER BY rowid", (rid,))
            return [{"id": row["id"], **json.loads(row["data"])} for row in rows]

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

    def request_cancel(self, rid):
        with self.db.connect() as con:
            changed = con.execute("UPDATE agent_runs SET cancel_requested=1 WHERE id=? AND status IN ('queued','running')", (rid,)).rowcount
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

    def import_paper(self, rid, eid):
        """Only source-derived metadata is importable; the model cannot supply arbitrary writes."""
        with self.db.connect() as con:
            con.execute("BEGIN IMMEDIATE")
            record = con.execute("SELECT data FROM agent_evidence WHERE run_id=? AND id=?", (rid, eid)).fetchone()
            if not record:
                raise HTTPException(404, "当前任务中不存在这条证据")
            source = json.loads(record[0])
            if not source.get("paper") or source.get("kind") != "paper":
                raise HTTPException(422, "此证据不是可导入的论文元数据")
            paper = PaperInput.model_validate(source["paper"])
            imported = con.execute("SELECT paper_id FROM agent_imports WHERE run_id=? AND evidence_id=?", (rid, eid)).fetchone()
            if imported and con.execute("SELECT id FROM papers WHERE id=?", (imported[0],)).fetchone():
                return {"paper_id": imported[0], "created": False}
            arxiv = re.sub(r"v\d+$", "", paper.arxiv_id)
            existing = con.execute("SELECT id FROM papers WHERE (doi != '' AND doi=?) OR (arxiv_base != '' AND arxiv_base=?)", (paper.doi, arxiv)).fetchone()
            if existing:
                pid, created = existing[0], False
            else:
                pid, stamp, created = str(uuid4()), now(), True
                con.execute("INSERT INTO papers VALUES (?,?,?,?,?,?,?)", (pid, encode(paper.model_dump(mode="json")), paper.doi, arxiv, stamp, stamp, 0))
            con.execute("INSERT OR REPLACE INTO agent_imports VALUES (?,?,?,?)", (rid, eid, pid, now()))
            con.execute("INSERT INTO agent_events(run_id,at,kind,data) VALUES (?,?,?,?)", (rid, now(), "approved_import", encode({"evidence_id": eid, "paper_id": pid, "created": created})))
        return {"paper_id": pid, "created": created}
