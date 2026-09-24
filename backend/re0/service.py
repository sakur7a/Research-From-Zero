"""Application services: explicit identities, safe imports, immutable evidence.

Every method here that touches a reader's library takes an `owner`, and it is a required keyword on
purpose. A default would let a new endpoint serve whoever's data the default names — and that default
would be the local owner's, which is exactly the library a hosted deployment must not hand to a
visitor. Required means an endpoint that forgets fails loudly in a test instead of quietly leaking.

The owner is never read from a request payload. It comes from the verified identity resolved in
`main.py` (`auth.Identity.owner`), so a body that declares an `owner_id` is describing something that
does not exist. Cross-owner access raises 404 rather than 403: a 403 confirms the id belongs to
somebody, which turns an id space into an enumeration of who has what.
"""
from __future__ import annotations

import json
import re
import sqlite3
from urllib.parse import urlsplit
from uuid import uuid4

from fastapi import HTTPException
from pydantic import ValidationError

from .db import Database, encode
from .knowledge import (append_observation_snapshot, append_source_snapshot,
                        encode as encode_knowledge_snapshot, ensure_paper_version,
                        fulltext_matches_version, metadata_snapshot,
                        sync_topic_assignments)
from .models import PaperInput, ResourceAudit, ResourceInput, ResourceKind, now, safe_url
from .models import ResearchRelationInput, ResearchTemplateDefinition, TopicAssignmentInput

# A record is either something a check found or something a person confirmed. There is no third
# kind: a model's reading of a check is stored as an observation with its inference inside it, not
# as its own authority.
RECORD_KINDS = ("observation", "confirmation")

# Resources and observations carry no owner column: they belong to a paper, and one source of truth
# beats two that can disagree. Every query on them therefore joins `papers` and filters there.
OWNED_RESOURCES = ("SELECT r.* FROM resources r JOIN papers p ON p.id=r.paper_id "
                   "WHERE p.owner=?")


def paper_record(row) -> dict:
    return {**json.loads(row["data"]), "id": row["id"], "created_at": row["created_at"],
            "updated_at": row["updated_at"], "is_demo": bool(row["is_demo"])}


def resource_record(row) -> dict:
    return {**json.loads(row["data"]), "id": row["id"], "paper_id": row["paper_id"], "created_at": row["created_at"]}


def _cited(evidence: list) -> str:
    """Flatten an audit's evidence into the one string a library resource record carries."""
    return " | ".join(f"{item.locator}: {item.excerpt} ({item.source_url})"
                      for item in evidence)[:4000]


def validation_message(exc: ValidationError) -> str:
    """The first reason a payload was refused, in the words the model wrote it in.

    The app-wide validation handler replaces every message with a generic one, because
    model-configuration payloads carry secrets and FastAPI echoes invalid input back. None of these
    records hold a secret, and "the form was wrong" is no answer for a reader who was asked to
    attach a source and did not.
    """
    (error,) = exc.errors()[:1]
    text = str(error.get("msg") or "").removeprefix("Value error, ").strip()
    return text[:300] or "输入格式或取值不符合要求"


def resource_from_audit(audit: ResourceAudit) -> ResourceInput:
    """A library resource from an audit row.

    The two share a vocabulary on purpose — `attribution` and `ownership`, `author_declaration` and
    `claim` — so this is a transcription rather than a re-judgement. Neither evidence field is
    dropped, because `ResourceInput` refuses an attribution or a claim that has none.
    """
    tail = [part for part in urlsplit(audit.resource_url).path.split("/") if part]
    return ResourceInput(
        kind=ResourceKind(audit.resource_type),
        label=("/".join(tail[-2:]) or audit.resource_url)[:200],
        url=audit.resource_url,
        ownership=audit.attribution,
        ownership_evidence=_cited(audit.attribution_evidence),
        claim=audit.author_declaration,
        claim_evidence=_cited(audit.author_declaration_evidence),
        applicable_version=audit.work_version,
    )


class Store:
    def __init__(self, db: Database):
        self.db = db

    def _resources(self, con, owner: str, paper_id: str = "") -> list[dict]:
        query, args = OWNED_RESOURCES, [owner]
        if paper_id:
            query += " AND r.paper_id=?"
            args.append(paper_id)
        rows = [resource_record(row) for row in
                con.execute(query + " ORDER BY r.created_at, r.id", args)]
        if not rows:
            return rows
        ids = {row["id"] for row in rows}
        latest = {row["resource_id"]: {**json.loads(row["data"]), "id": row["id"]} for row in con.execute(
            """SELECT o.* FROM observations o JOIN
               (SELECT resource_id, MAX(id) mid FROM observations GROUP BY resource_id) recent
               ON recent.mid=o.id""") if row["resource_id"] in ids}
        for row in rows:
            row["latest"] = latest.get(row["id"])
        return rows

    def list_papers(self, *, owner: str) -> list[dict]:
        with self.db.connect() as con:
            papers = [paper_record(row) for row in con.execute(
                "SELECT * FROM papers WHERE owner=? ORDER BY updated_at DESC, id", (owner,))]
            by_paper: dict[str, list] = {}
            for resource in self._resources(con, owner):
                by_paper.setdefault(resource["paper_id"], []).append(resource)
            for paper in papers:
                paper["resources"] = by_paper.get(paper["id"], [])
            return papers

    def get_paper(self, paper_id: str, *, owner: str) -> dict:
        with self.db.connect() as con:
            row = con.execute("SELECT * FROM papers WHERE id=? AND owner=?",
                              (paper_id, owner)).fetchone()
            if row is None:
                raise HTTPException(404, "论文不存在")
            paper = paper_record(row)
            paper["resources"] = self._resources(con, owner, paper_id)
            return paper

    def create_paper(self, data: PaperInput, *, owner: str, demo: bool = False) -> dict:
        paper_id, timestamp = str(uuid4()), now()
        try:
            with self.db.connect() as con:
                con.execute("INSERT INTO papers(id,data,doi,arxiv_base,created_at,updated_at,"
                            "is_demo,owner) VALUES (?,?,?,?,?,?,?,?)", (
                                paper_id, encode(data.model_dump(mode="json")), data.doi,
                                re.sub(r"v\d+$", "", data.arxiv_id), timestamp, timestamp,
                                int(demo), owner))
                con.executemany("INSERT OR IGNORE INTO topics(name,owner) VALUES (?,?)",
                                [(name, owner) for name in data.topics])
                paper_data = data.model_dump(mode="json")
                version_id = ensure_paper_version(con, paper_id, owner, paper_data,
                                                  origin="demo" if demo else "manual_entry",
                                                  created_at=timestamp)
                append_source_snapshot(con, work_id=paper_id, owner=owner,
                                       paper_version_id=version_id, kind="paper_metadata",
                                       payload=metadata_snapshot(paper_data),
                                       retrieved_at=timestamp, source_url=data.paper_url,
                                       locator="library entry")
                sync_topic_assignments(con, paper_id, owner, data.topics, timestamp)
        except sqlite3.IntegrityError as exc:
            raise HTTPException(409, "此 DOI 或 arXiv 已在文献库中；请编辑已有条目，原有笔记不会被覆盖") from exc
        return self.get_paper(paper_id, owner=owner)

    def update_paper(self, paper_id: str, data: PaperInput, *, owner: str) -> dict:
        try:
            with self.db.connect() as con:
                previous = con.execute("SELECT data,created_at FROM papers WHERE id=? AND owner=?",
                                       (paper_id, owner)).fetchone()
                if previous is None:
                    raise HTTPException(404, "论文不存在")
                previous_data = json.loads(previous["data"])
                timestamp = now()
                result = con.execute("UPDATE papers SET data=?,doi=?,arxiv_base=?,updated_at=? "
                                     "WHERE id=? AND owner=?", (
                                         encode(data.model_dump(mode="json")), data.doi,
                                         re.sub(r"v\d+$", "", data.arxiv_id), timestamp, paper_id, owner))
                if not result.rowcount:
                    raise HTTPException(404, "论文不存在")
                con.executemany("INSERT OR IGNORE INTO topics(name,owner) VALUES (?,?)",
                                [(name, owner) for name in data.topics])
                paper_data = data.model_dump(mode="json")
                version_id = ensure_paper_version(con, paper_id, owner, paper_data,
                                                  origin="manual_edit", created_at=timestamp)
                current_metadata = metadata_snapshot(paper_data)
                if metadata_snapshot(previous_data) != current_metadata:
                    append_source_snapshot(con, work_id=paper_id, owner=owner,
                                           paper_version_id=version_id, kind="paper_metadata_edit",
                                           payload=current_metadata, retrieved_at=timestamp,
                                           source_url=data.paper_url, locator="library edit")
                sync_topic_assignments(con, paper_id, owner, data.topics, timestamp)
        except sqlite3.IntegrityError as exc:
            raise HTTPException(409, "此 DOI 或 arXiv 已被另一篇论文使用；没有覆盖任何条目") from exc
        return self.get_paper(paper_id, owner=owner)

    def delete_paper(self, paper_id: str, *, owner: str):
        with self.db.connect() as con:
            if not con.execute("DELETE FROM papers WHERE id=? AND owner=?",
                               (paper_id, owner)).rowcount:
                raise HTTPException(404, "论文不存在")

    def create_resource(self, paper_id: str, data: ResourceInput, *, owner: str) -> dict:
        resource_id = str(uuid4())
        with self.db.connect() as con:
            if not con.execute("SELECT 1 FROM papers WHERE id=? AND owner=?",
                               (paper_id, owner)).fetchone():
                raise HTTPException(404, "论文不存在")
            con.execute("INSERT INTO resources VALUES (?,?,?,?)",
                        (resource_id, paper_id, encode(data.model_dump(mode="json")), now()))
            con.execute("UPDATE papers SET updated_at=? WHERE id=? AND owner=?", (now(), paper_id, owner))
        return self.get_resource(resource_id, owner=owner)

    def get_resource(self, resource_id: str, *, owner: str) -> dict:
        with self.db.connect() as con:
            row = con.execute(OWNED_RESOURCES + " AND r.id=?", (owner, resource_id)).fetchone()
            if not row:
                raise HTTPException(404, "资源不存在")
            return resource_record(row)

    def history(self, resource_id: str, *, owner: str) -> list[dict]:
        self.get_resource(resource_id, owner=owner)
        with self.db.connect() as con:
            rows = [{**json.loads(row["data"]), "id": row["id"]} for row in con.execute(
                "SELECT * FROM observations WHERE resource_id=? ORDER BY id DESC", (resource_id,))]
        # A record written before kinds existed was written by the only writer there was: a check.
        # Defaulting it to anything else would rewrite history rather than read it.
        return [{"record_kind": "observation", **row} for row in rows]

    def save_observation(self, resource_id: str, observation: dict, *, owner: str,
                         kind: str = "observation", origin: str = "check"):
        """Append one immutable record, labelled with who is speaking.

        A machine check and a human confirmation are different kinds of statement, and an
        append-only table with nothing on the row to tell them apart is how a guess gets read back
        later as an observation. `origin` separates a check this service performed from one a user
        imported from elsewhere, which this process never verified.
        """
        if kind not in RECORD_KINDS:
            raise HTTPException(422, f"记录类型只能是 {' 或 '.join(RECORD_KINDS)}")
        record = {**observation, "record_kind": kind, "record_origin": origin, "recorded_at": now()}
        try:
            with self.db.connect() as con:
                scoped = con.execute("SELECT r.data AS resource_data,r.paper_id,p.data AS paper_data "
                                     "FROM resources r JOIN papers p ON p.id=r.paper_id "
                                     "WHERE r.id=? AND p.owner=?", (resource_id, owner)).fetchone()
                if scoped is None:
                    raise HTTPException(404, "资源不存在")
                checked_at = record.get("checked_at") or now()
                cursor = con.execute("INSERT INTO observations(resource_id,data,checked_at) VALUES (?,?,?)",
                                     (resource_id, encode(record), checked_at))
                append_observation_snapshot(con, observation_id=cursor.lastrowid,
                                            work_id=scoped["paper_id"], owner=owner,
                                            paper=json.loads(scoped["paper_data"]),
                                            resource=json.loads(scoped["resource_data"]),
                                            observation=record, retrieved_at=checked_at)
        except sqlite3.IntegrityError as exc:
            raise HTTPException(409, "检查期间资源已被删除，结果未写入其他资源") from exc
        return self.history(resource_id, owner=owner)[0]

    def confirm_resource(self, resource_id: str, revision: ResourceAudit, *, owner: str) -> dict:
        """A human confirmation, stored as its own record beside the observations it revises.

        This is the only writer that may carry a settled attribution or a version judgement, and the
        model behind it refuses both without a source. Nothing a model or an external host returned
        reaches here: a `confirmed: true` inside a tool result is a string in some payload, not a
        user's approval, and there is no argument that turns one into the other.
        """
        resource = self.get_resource(resource_id, owner=owner)
        if resource["url"] != revision.resource_url:
            raise HTTPException(422, "确认的内容必须对应这个资源的链接")
        return self.save_observation(resource_id, revision.model_dump(mode="json"), owner=owner,
                                     kind="confirmation", origin="user")

    def import_audits(self, items: list[dict], dry_run: bool = True, *, owner: str,
                      agent_associations: list[list[dict]] | None = None) -> dict:
        """Link approved papers and their resource audits into the library.

        Idempotent by identifier, by resource URL and by audit fingerprint, and it never updates a
        paper that is already here: an existing record carries the reader's own notes, and an
        import that overwrote them would be a loss nobody asked for. Only source-derived metadata is
        accepted, so an approval cannot be used to invent a paper.
        """
        if len(items) > 200:
            raise HTTPException(422, "单次最多导入 200 篇论文的审计结果")
        prepared, errors = [], []
        for index, item in enumerate(items):
            try:
                if not isinstance(item, dict):
                    raise ValueError("每条记录必须是 JSON 对象")
                paper = PaperInput.model_validate(item.get("paper") or {})
                audits = [ResourceAudit.model_validate(row) for row in (item.get("audits") or [])]
                if not audits:
                    raise ValueError("没有可导入的审计记录")
                prepared.append((index, paper, audits))
            except (ValidationError, ValueError, TypeError) as exc:
                reason = validation_message(exc) if isinstance(exc, ValidationError) else str(exc)
                errors.append({"index": index, "message": reason or "审计记录格式错误"})
        created, linked, skipped = [], [], []
        if not dry_run:
            for index, paper, audits in prepared:
                linkable = [row for row in audits if row.resource_type != "unknown"]
                for row in audits:
                    if row.resource_type == "unknown":
                        # An unsupported link was never checked, so there is nothing to link.
                        skipped.append({"index": index, "url": row.resource_url,
                                        "reason": "链接类型不受支持，未产生审计结论"})
                if not linkable:
                    # Nothing to link, so nothing to create. A paper this import cannot attach a
                    # single resource to is a record nobody asked the library to hold.
                    continue
                paper_id, was_new = self._paper_for_import(paper, owner=owner)
                if was_new:
                    created.append({"index": index, "paper_id": paper_id, "title": paper.title})
                known = {item["url"]: item["id"]
                         for item in self.get_paper(paper_id, owner=owner)["resources"]}
                for audit in linkable:
                    resource_id = known.get(audit.resource_url)
                    if resource_id is None:
                        resource_id = self.create_resource(
                            paper_id, resource_from_audit(audit), owner=owner)["id"]
                        known[audit.resource_url] = resource_id
                    fingerprint = (audit.checked_at, audit.revision, audit.status)
                    if any((row.get("checked_at"), row.get("revision"), row.get("status")) == fingerprint
                           for row in self.history(resource_id, owner=owner)):
                        skipped.append({"index": index, "url": audit.resource_url,
                                        "reason": "同一次检查已导入过"})
                        continue
                    observation = audit.model_dump(mode="json")
                    item_associations = (agent_associations[index]
                                         if agent_associations and index < len(agent_associations) else [])
                    approvals = []
                    for proposal in item_associations:
                        if audit.resource_url not in proposal.get("resource_urls", []):
                            continue
                        sources = []
                        for source in proposal.get("sources", [])[:8]:
                            if not isinstance(source, dict):
                                continue
                            try:
                                source_url = safe_url(str(source.get("source_url") or ""))
                            except ValueError:
                                continue
                            evidence_id = str(source.get("evidence_id") or "")[:100]
                            if not evidence_id:
                                continue
                            sources.append({"evidence_id": evidence_id,
                                            "source_url": source_url,
                                            "locator": str(source.get("locator") or "")[:400],
                                            "retrieved_at": str(source.get("retrieved_at") or "")[:60]})
                        evidence_ids = list(dict.fromkeys(
                            str(value)[:100] for value in proposal.get("evidence_ids", [])
                            if value))[:8]
                        if not evidence_ids or not sources:
                            continue
                        approvals.append({
                            "run_id": str(proposal.get("run_id") or "")[:80],
                            "paper_evidence_id": str(proposal.get("paper_evidence_id") or "")[:100],
                            "resource_evidence_id": str(proposal.get("resource_evidence_id") or "")[:100],
                            "resource_url": audit.resource_url,
                            "resource_checked_at": audit.checked_at,
                            "resource_scope": audit.scope,
                            "proposal_evidence_ids": evidence_ids,
                            "proposal_sources": sources,
                            "proposal_note": str(proposal.get("rationale") or "")[:600],
                            "decision": "confirmed_by_user",
                            "confirmed_at": now(),
                        })
                    if approvals:
                        observation["agent_association_approvals"] = approvals
                    self.save_observation(resource_id, observation, owner=owner,
                                          kind="observation", origin="import")
                    linked.append({"index": index, "paper_id": paper_id,
                                   "resource_id": resource_id, "url": audit.resource_url,
                                   "status": audit.status})
        return {"dry_run": dry_run, "ready": len(prepared), "errors": errors, "skipped": skipped,
                "created": created, "linked": linked,
                "preview": [{"index": index, "title": paper.title,
                             "resources": [row.resource_url for row in audits]}
                            for index, paper, audits in prepared[:20]],
                "note": "导入只建立关联，不修改已存在论文的笔记或字段；确认归属是另一条记录。"}

    def _paper_for_import(self, paper: PaperInput, *, owner: str) -> tuple:
        """Find the paper by identifier, or create it. An existing one is returned untouched.

        The lookup is scoped, so an identifier another owner holds is not found here and becomes this
        owner's own row. Sharing a DOI is not a reason to share a record, and the answer must not
        reveal that somebody else already has it.
        """
        arxiv = re.sub(r"v\d+$", "", paper.arxiv_id)
        with self.db.connect() as con:
            row = con.execute("SELECT id FROM papers WHERE owner=? AND ((doi != '' AND doi=?) "
                              "OR (arxiv_base != '' AND arxiv_base=?))",
                              (owner, paper.doi, arxiv)).fetchone()
        if row:
            return row[0], False
        return self.create_paper(paper, owner=owner)["id"], True

    def delete_resource(self, resource_id: str, *, owner: str):
        # The subquery is the authorization: without it an id from another library would delete a row
        # this caller was never shown.
        with self.db.connect() as con:
            if not con.execute("DELETE FROM resources WHERE id=? AND paper_id IN "
                               "(SELECT id FROM papers WHERE owner=?)",
                               (resource_id, owner)).rowcount:
                raise HTTPException(404, "资源不存在")

    def topics(self, *, owner: str) -> list[str]:
        with self.db.connect() as con:
            return [row[0] for row in con.execute("SELECT name FROM topics WHERE owner=? "
                                                  "ORDER BY name", (owner,))]

    def add_topic(self, name: str, *, owner: str):
        with self.db.connect() as con:
            con.execute("INSERT OR IGNORE INTO topics(name,owner) VALUES (?,?)", (name, owner))

    def templates(self, *, owner: str) -> list[dict]:
        from .knowledge import SYSTEM_TEMPLATE_OWNER

        with self.db.connect() as con:
            rows = con.execute("SELECT id,owner,template_key,version,name,definition,created_at "
                               "FROM topic_template_versions WHERE owner IN (?,?) "
                               "ORDER BY template_key,version", (SYSTEM_TEMPLATE_OWNER, owner))
            result = []
            for row in rows:
                definition = json.loads(row["definition"])
                definition.pop("template_key", None)
                definition.pop("version", None)
                result.append({"id": row["id"], "template_key": row["template_key"],
                               "version": row["version"], "name": row["name"],
                               "definition": definition, "created_at": row["created_at"],
                               "editable": row["owner"] == owner})
            return result

    def create_template_version(self, template_key: str, data: ResearchTemplateDefinition,
                                *, owner: str) -> dict:
        if not re.fullmatch(r"[a-z][a-z0-9_-]{0,79}", template_key):
            raise HTTPException(422, "模板 key 只能使用小写字母、数字、下划线或连字符")
        from .knowledge import SYSTEM_TEMPLATE_OWNER

        template_id, stamp = str(uuid4()), now()
        with self.db.connect() as con:
            if owner == SYSTEM_TEMPLATE_OWNER:
                raise HTTPException(403, "系统模板不可由请求覆盖")
            latest = con.execute("SELECT MAX(version) FROM topic_template_versions "
                                 "WHERE template_key=? AND owner IN (?,?)",
                                 (template_key, SYSTEM_TEMPLATE_OWNER, owner)).fetchone()[0]
            version = (latest or 0) + 1
            con.execute("INSERT INTO topic_template_versions(id,owner,template_key,version,name,"
                        "definition,created_at) VALUES (?,?,?,?,?,?,?)",
                        (template_id, owner, template_key, version, data.name,
                         encode(data.model_dump(mode="json")), stamp))
        return next(item for item in self.templates(owner=owner) if item["id"] == template_id)

    def assign_template_topic(self, paper_id: str, data: TopicAssignmentInput, *, owner: str) -> dict:
        from .knowledge import SYSTEM_TEMPLATE_OWNER

        assignment_id, stamp = str(uuid4()), now()
        with self.db.connect() as con:
            paper_row = con.execute("SELECT data FROM papers WHERE id=? AND owner=?",
                                    (paper_id, owner)).fetchone()
            if not paper_row:
                raise HTTPException(404, "论文不存在")
            template = con.execute("SELECT owner,definition FROM topic_template_versions WHERE id=? "
                                   "AND owner IN (?,?)",
                                   (data.template_version_id, SYSTEM_TEMPLATE_OWNER, owner)).fetchone()
            if not template:
                raise HTTPException(404, "方向模板版本不存在")
            definition = json.loads(template["definition"])
            concept = next((item for item in definition["concepts"]
                            if item["key"] == data.concept_key), None)
            if not concept:
                raise HTTPException(422, "概念不属于所选模板版本")
            existing = con.execute("SELECT id FROM paper_topic_assignments WHERE paper_id=? "
                                   "AND template_version_id=? AND concept_key=?",
                                   (paper_id, data.template_version_id, data.concept_key)).fetchone()
            if existing:
                return self._topic_assignment(con, existing[0], owner)
            label = concept["label"]
            con.execute("INSERT INTO paper_topic_assignments(id,owner,paper_id,template_version_id,"
                        "concept_key,label,source_kind,created_at) VALUES (?,?,?,?,?,?,?,?)",
                        (assignment_id, owner, paper_id, data.template_version_id,
                         data.concept_key, label, "user_template_selection", stamp))
            paper = json.loads(paper_row["data"])
            labels = list(dict.fromkeys([*(paper.get("topics") or []), label]))
            paper["topics"] = labels
            con.execute("UPDATE papers SET data=?,updated_at=? WHERE id=? AND owner=?",
                        (encode(paper), stamp, paper_id, owner))
            con.execute("INSERT OR IGNORE INTO topics(name,owner) VALUES (?,?)", (label, owner))
        with self.db.connect() as con:
            return self._topic_assignment(con, assignment_id, owner)

    @staticmethod
    def _topic_assignment(con, assignment_id: str, owner: str) -> dict:
        row = con.execute("SELECT a.*,t.template_key,t.version AS template_version,t.definition "
                          "FROM paper_topic_assignments a LEFT JOIN topic_template_versions t "
                          "ON t.id=a.template_version_id WHERE a.id=? AND a.owner=?",
                          (assignment_id, owner)).fetchone()
        if not row:
            raise HTTPException(404, "方向关联不存在")
        concept = None
        if row["definition"]:
            concept = next((item for item in json.loads(row["definition"])["concepts"]
                            if item["key"] == row["concept_key"]), None)
        result = dict(row)
        result.pop("definition", None)
        result.pop("owner", None)
        result["concept"] = concept
        return result

    def remove_topic_assignment(self, paper_id: str, assignment_id: str, *, owner: str) -> None:
        with self.db.connect() as con:
            row = con.execute("SELECT label FROM paper_topic_assignments WHERE id=? AND paper_id=? "
                              "AND owner=?", (assignment_id, paper_id, owner)).fetchone()
            if not row:
                raise HTTPException(404, "方向关联不存在")
            con.execute("DELETE FROM paper_topic_assignments WHERE id=? AND paper_id=? AND owner=?",
                        (assignment_id, paper_id, owner))
            remaining = con.execute("SELECT 1 FROM paper_topic_assignments WHERE paper_id=? AND label=?",
                                    (paper_id, row["label"])).fetchone()
            paper_row = con.execute("SELECT data FROM papers WHERE id=? AND owner=?",
                                    (paper_id, owner)).fetchone()
            if paper_row and not remaining:
                paper = json.loads(paper_row["data"])
                paper["topics"] = [label for label in paper.get("topics", []) if label != row["label"]]
                con.execute("UPDATE papers SET data=?,updated_at=? WHERE id=? AND owner=?",
                            (encode(paper), now(), paper_id, owner))

    def import_fulltext_chunks(self, paper_id: str, paper_version_id: str,
                               sources: list[tuple[str, dict]], *, owner: str,
                               apply: bool = False) -> dict:
        """Preview or append user-confirmed, version-matched full-text workspace chunks."""
        result = {"applied": bool(apply), "paper_id": paper_id,
                  "paper_version_id": paper_version_id, "new": [],
                  "already_present": [], "conflicts": [], "provenance_verified": False,
                  "note": "全文来自用户导入的工作区 bundle；来源声明未经加密认证。"}
        with self.db.connect() as con:
            if apply:
                # Serialize the idempotency check and inserts; a retried POST cannot add the same
                # source twice even when two requests arrive together.
                con.execute("BEGIN IMMEDIATE")
            paper = con.execute("SELECT 1 FROM papers WHERE id=? AND owner=?",
                                (paper_id, owner)).fetchone()
            if not paper:
                raise HTTPException(404, "论文不存在")
            version_row = con.execute("SELECT * FROM paper_versions WHERE id=? AND work_id=?",
                                      (paper_version_id, paper_id)).fetchone()
            if not version_row:
                raise HTTPException(404, "论文版本不存在")
            version = dict(version_row)

            for source_id, source in sources:
                fulltext = source.get("fulltext") if isinstance(source, dict) else None
                content = source.get("content") if isinstance(source, dict) else None
                locator = source.get("locator") if isinstance(source, dict) else None
                try:
                    source_url = safe_url(source.get("source_url", "")) if isinstance(source, dict) else ""
                except ValueError:
                    source_url = ""
                reason = ""
                if (not isinstance(source, dict) or source.get("kind") != "fulltext_chunk"
                        or source.get("tool") != "fetch_paper_text"):
                    reason = "所选来源不是 fetch_paper_text 生成的全文块"
                elif not isinstance(fulltext, dict):
                    reason = "全文来源缺少一致的文档版本／解析元数据"
                elif fulltext.get("state") not in {"ok", "partial"}:
                    reason = "全文读取状态不支持导入"
                elif not source_url or source_url != fulltext.get("final_url"):
                    reason = "全文来源链接无效或与读取记录不一致"
                elif not isinstance(locator, str) or not locator.strip():
                    reason = "全文块没有可复查的段落／页码定位"
                elif not isinstance(content, str) or not content.strip() or len(content.encode("utf-8")) > 64 * 1024:
                    reason = "全文块正文为空或超过 64 KiB"
                elif not fulltext_matches_version(fulltext, version):
                    reason = "全文标识符或版本与所选论文版本不匹配"
                if reason:
                    result["conflicts"].append({"source_id": source_id, "reason": reason})
                    continue

                existing = con.execute(
                    "SELECT id FROM source_snapshots WHERE owner=? AND work_id=? "
                    "AND snapshot_kind='fulltext_chunk' "
                    "AND json_extract(payload,'$.workspace_source_id')=? LIMIT 1",
                    (owner, paper_id, source_id)).fetchone()
                if existing:
                    result["already_present"].append({"source_id": source_id,
                                                       "source_snapshot_id": existing["id"],
                                                       "locator": locator})
                    continue

                summary = {"source_id": source_id, "source_url": source_url,
                           "locator": locator,
                           "retrieved_at": str(fulltext.get("fetched_at") or source.get("retrieved_at") or ""),
                           "fulltext": fulltext,
                           "provenance_verified": not bool(source.get("imported_by_user"))}
                if apply:
                    snapshot_payload = {
                        "workspace_source_id": source_id,
                        "source_origin": source.get("origin", "tool"),
                        "source_tool": source.get("tool", ""),
                        "imported_by_user": bool(source.get("imported_by_user")),
                        "provenance_verified": summary["provenance_verified"],
                        "paper": source.get("paper") or {},
                        "fulltext": fulltext,
                        "content": content,
                    }
                    snapshot_id = append_source_snapshot(
                        con, work_id=paper_id, owner=owner, paper_version_id=paper_version_id,
                        kind="fulltext_chunk", payload=snapshot_payload,
                        retrieved_at=summary["retrieved_at"] or now(),
                        source_url=source_url, locator=locator)
                    result["new"].append({**summary, "source_snapshot_id": snapshot_id})
                else:
                    result["new"].append({**summary, "content": content})
        return result

    def add_relation(self, paper_id: str, data: ResearchRelationInput, *, owner: str) -> dict:
        relation_id, stamp = str(uuid4()), now()
        with self.db.connect() as con:
            if not con.execute("SELECT 1 FROM works WHERE id=? AND owner=?", (paper_id, owner)).fetchone():
                raise HTTPException(404, "论文不存在")
            source = con.execute("SELECT source_url,locator,payload,snapshot_kind,paper_version_id "
                                 "FROM source_snapshots "
                                 "WHERE id=? AND owner=? "
                                 "AND work_id=?", (data.source_snapshot_id, owner, paper_id)).fetchone()
            if not source:
                raise HTTPException(404, "来源快照不存在")
            if not source["source_url"]:
                raise HTTPException(422, "该快照没有可跳转来源，不能支持关系判断")
            if data.relation_type == "claim" and source["snapshot_kind"] not in {
                    "fulltext", "fulltext_chunk", "fulltext_snapshot"}:
                raise HTTPException(422, "claim 关系需要 #8 的全文来源与段落定位；元数据和仓库观察不够")
            if data.relation_type == "claim" and not source["paper_version_id"]:
                raise HTTPException(422, "claim 关系需要明确绑定到同一论文版本的全文快照")
            if data.relation_type == "claim":
                snapshot_payload = json.loads(source["payload"])
                if source["snapshot_kind"] == "fulltext_chunk":
                    located_text = snapshot_payload.get("content") or ""
                    if f"[{data.locator}]" not in located_text:
                        raise HTTPException(422, "claim 的定位必须是所选全文块中的实际段落／页码 locator")
                elif data.locator != source["locator"]:
                    raise HTTPException(422, "claim 的定位必须与所选全文快照一致")
                if (snapshot_payload.get("provenance_verified") is False
                        and data.assertion_kind != "human_confirmation"):
                    raise HTTPException(422, "导入全文的来源声明未经认证；请由人工核对后再记录 claim")
            version_id = data.paper_version_id or source["paper_version_id"] or None
            if data.paper_version_id:
                if not source["paper_version_id"]:
                    raise HTTPException(422, "来源快照未绑定论文版本，不能据此写入版本关系")
                if data.paper_version_id != source["paper_version_id"]:
                    raise HTTPException(422, "关系版本必须与来源快照绑定的版本一致")
            if version_id and not con.execute("SELECT 1 FROM paper_versions WHERE id=? AND work_id=?",
                                              (version_id, paper_id)).fetchone():
                raise HTTPException(422, "论文版本不属于这个 Work")
            supersedes = data.supersedes_id or None
            if supersedes and not con.execute("SELECT 1 FROM research_relations WHERE id=? AND owner=? "
                                              "AND work_id=?", (supersedes, owner, paper_id)).fetchone():
                raise HTTPException(404, "待修订关系不存在")
            con.execute("INSERT INTO research_relations(id,owner,work_id,paper_version_id,relation_type,"
                        "target,statement,conditions,assertion_kind,source_snapshot_id,locator,created_at,"
                        "supersedes_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (relation_id, owner, paper_id, version_id, data.relation_type, data.target,
                         data.statement, data.conditions, data.assertion_kind, data.source_snapshot_id,
                         data.locator, stamp, supersedes))
        return next(item for item in self.knowledge(paper_id, owner=owner)["relations"]
                    if item["id"] == relation_id)

    def knowledge(self, paper_id: str, *, owner: str) -> dict:
        if not self.get_paper(paper_id, owner=owner):
            raise HTTPException(404, "论文不存在")
        with self.db.connect() as con:
            work = con.execute("SELECT id,owner,created_at FROM works WHERE id=? AND owner=?",
                               (paper_id, owner)).fetchone()
            versions = [dict(row) for row in con.execute(
                "SELECT id,version_key,arxiv_id,version_label,doi,metadata,origin,created_at,is_current "
                "FROM paper_versions WHERE work_id=? ORDER BY created_at,id", (paper_id,))]
            for row in versions:
                row["metadata"] = json.loads(row["metadata"])
            snapshots = [dict(row) for row in con.execute(
                "SELECT id,paper_version_id,snapshot_kind,source_url,locator,payload,content_sha256,"
                "retrieved_at,observation_id FROM source_snapshots WHERE owner=? AND work_id=? "
                "ORDER BY retrieved_at,id", (owner, paper_id))]
            for row in snapshots:
                row["payload"] = json.loads(row["payload"])
            assignments = [self._topic_assignment(con, row[0], owner) for row in con.execute(
                "SELECT id FROM paper_topic_assignments WHERE paper_id=? AND owner=? ORDER BY label,id",
                (paper_id, owner))]
            relations = [dict(row) for row in con.execute(
                "SELECT r.*,s.source_url,s.content_sha256 FROM research_relations r "
                "JOIN source_snapshots s ON s.id=r.source_snapshot_id "
                "WHERE r.work_id=? AND r.owner=? ORDER BY r.created_at,r.id", (paper_id, owner))]
        for relation in relations:
            relation.pop("owner", None)
        work_view = ({"id": work["id"], "created_at": work["created_at"]}
                     if work else {"id": paper_id})
        return {"schema_version": 1, "work": work_view,
                "versions": versions, "source_snapshots": snapshots,
                "topic_assignments": assignments, "relations": relations}

    def list_relations(self, *, owner: str, paper_id: str = "", relation_type: str = "",
                       query: str = "", limit: int = 50, offset: int = 0) -> dict:
        if paper_id:
            self.get_paper(paper_id, owner=owner)
        clauses = ["r.owner=?"]
        args: list[object] = [owner]
        if paper_id:
            clauses.append("r.work_id=?")
            args.append(paper_id)
        if relation_type:
            clauses.append("r.relation_type=?")
            args.append(relation_type)
        query = query.strip()
        if query:
            clauses.append("(r.target LIKE ? OR r.statement LIKE ? OR r.conditions LIKE ? "
                           "OR json_extract(p.data,'$.title') LIKE ?)")
            pattern = f"%{query}%"
            args.extend([pattern] * 4)
        where = " AND ".join(clauses)
        with self.db.connect() as con:
            total = con.execute("SELECT COUNT(*) FROM research_relations r "
                                "JOIN papers p ON p.id=r.work_id WHERE " + where, args).fetchone()[0]
            rows = [dict(row) for row in con.execute(
                "SELECT r.id,r.work_id,r.paper_version_id,r.relation_type,r.target,r.statement,"
                "r.conditions,r.assertion_kind,r.source_snapshot_id,r.locator,r.created_at,"
                "r.supersedes_id,json_extract(p.data,'$.title') AS paper_title,"
                "v.version_label,s.source_url,s.snapshot_kind,s.content_sha256 "
                "FROM research_relations r JOIN papers p ON p.id=r.work_id "
                "LEFT JOIN paper_versions v ON v.id=r.paper_version_id "
                "JOIN source_snapshots s ON s.id=r.source_snapshot_id WHERE " + where +
                " ORDER BY r.created_at DESC,r.id LIMIT ? OFFSET ?",
                [*args, limit, offset])]
        return {"schema_version": 1,
                "requested": {"paper_id": paper_id, "relation_type": relation_type,
                              "query": query, "limit": limit, "offset": offset},
                "total": total, "relations": rows}

    def template_topics(self, *, owner: str) -> list[dict]:
        return self.templates(owner=owner)

    def knowledge_export(self, *, owner: str) -> dict:
        papers = self.list_papers(owner=owner)
        return [self.knowledge(paper["id"], owner=owner) for paper in papers]

    def export(self, *, owner: str) -> dict:
        papers = self.list_papers(owner=owner)
        for paper in papers:
            for resource in paper["resources"]:
                resource["observations"] = self.history(resource["id"], owner=owner)
        # `owner` is in the export because a file that does not say whose library it is can be
        # imported into somebody else's, and then the answer is wrong in a way nobody can see.
        return {"schema_version": 3, "exported_at": now(), "owner": owner,
                "topics": self.topics(owner=owner), "papers": papers,
                "knowledge": self.knowledge_export(owner=owner)}

    def import_csl(self, items: list[dict], dry_run: bool = True, *, owner: str) -> dict:
        if len(items) > 500:
            raise HTTPException(422, "单次最多导入 500 条 CSL JSON 记录")
        existing = self.list_papers(owner=owner)
        by_doi, by_arxiv = {}, {}

        def arxiv_base(value: str) -> str:
            return re.sub(r"v\d+$", "", value, flags=re.I).casefold()

        def reference(*, paper_id: str = "", index: int | None = None,
                      paper: dict) -> dict:
            return {"paper_id": paper_id, "index": index, "paper": paper}

        for paper in existing:
            ref = reference(paper_id=paper["id"], paper=paper)
            if paper["doi"]:
                by_doi[paper["doi"].casefold()] = ref
            if paper["arxiv_id"]:
                by_arxiv[arxiv_base(paper["arxiv_id"])] = ref

        prepared, skipped, errors, conflicts = [], [], [], []
        for index, item in enumerate(items):
            try:
                if not isinstance(item, dict):
                    raise ValueError("记录必须是 JSON 对象")
                authors = [" ".join(filter(None, [a.get("given"), a.get("family")])) or a.get("literal", "Unknown") for a in item.get("author", [])]
                dates = item.get("issued", {}).get("date-parts", [[]])
                identifier = str(item.get("URL", ""))
                arxiv = ""
                if re.match(r"^https?://(?:www\.)?arxiv\.org/(?:abs|pdf)/", identifier):
                    arxiv = identifier
                data = PaperInput(title=item.get("title", ""), authors=authors,
                                  year=dates[0][0] if dates and dates[0] else None,
                                  doi=item.get("DOI", ""), arxiv_id=arxiv,
                                  paper_url=identifier, abstract=item.get("abstract", ""),
                                  venue=item.get("container-title", ""))
                base = arxiv_base(data.arxiv_id)
                candidates: dict[tuple, dict] = {}
                if data.doi and data.doi.casefold() in by_doi:
                    ref = by_doi[data.doi.casefold()]
                    key = ("work", ref["paper_id"]) if ref["paper_id"] else ("item", ref["index"])
                    candidates.setdefault(key, {"ref": ref, "matched_by": set()})["matched_by"].add("DOI")
                if base and base in by_arxiv:
                    ref = by_arxiv[base]
                    key = ("work", ref["paper_id"]) if ref["paper_id"] else ("item", ref["index"])
                    candidates.setdefault(key, {"ref": ref, "matched_by": set()})["matched_by"].add("arXiv")

                if candidates:
                    matches = list(candidates.values())
                    reasons = []
                    if len(matches) > 1:
                        reasons.append("导入 DOI 与 arXiv 分别指向不同文献")
                    for match in matches:
                        previous = match["ref"]["paper"]
                        previous_arxiv = previous.get("arxiv_id", "")
                        if (data.arxiv_id and previous_arxiv and
                                arxiv_base(data.arxiv_id) == arxiv_base(previous_arxiv) and
                                data.arxiv_id.casefold() != previous_arxiv.casefold()):
                            reasons.append("同一 arXiv 编号的版本不同")
                        if ("DOI" in match["matched_by"] and data.arxiv_id and
                                data.arxiv_id.casefold() != previous_arxiv.casefold()):
                            reasons.append("该条目尚未确认导入的 arXiv 对应关系")
                        if ("arXiv" in match["matched_by"] and data.doi and
                                data.doi.casefold() != str(previous.get("doi", "")).casefold()):
                            reasons.append("该条目尚未确认导入的 DOI 对应关系")

                    if reasons:
                        safe_declaration = {key: getattr(data, key) for key in
                                            ("title", "authors", "year", "venue", "doi", "arxiv_id", "paper_url")}
                        conflicts.append({
                            "index": index,
                            "title": data.title,
                            "reason": "；".join(dict.fromkeys(reasons)),
                            "declaration": safe_declaration,
                            "matches": [{
                                "work_id": match["ref"]["paper_id"],
                                "item_index": match["ref"]["index"],
                                "title": match["ref"]["paper"]["title"],
                                "doi": match["ref"]["paper"].get("doi", ""),
                                "arxiv_id": match["ref"]["paper"].get("arxiv_id", ""),
                                "matched_by": sorted(match["matched_by"]),
                                "_reference": match["ref"],
                            } for match in matches],
                            "_references": [match["ref"] for match in matches],
                            "recorded_work_ids": [],
                            "recorded_snapshots": [],
                        })
                        continue

                    skipped.append({"index": index, "title": data.title,
                                    "reason": "DOI 或 arXiv 已存在；未覆盖现有记录"})
                    continue

                row = {"index": index, "data": data}
                prepared.append(row)
                pending_ref = reference(index=index, paper=data.model_dump(mode="json"))
                row["_reference"] = pending_ref
                if data.doi:
                    by_doi[data.doi.casefold()] = pending_ref
                if base:
                    by_arxiv[base] = pending_ref
            except (ValidationError, ValueError, TypeError, KeyError, IndexError, AttributeError):
                errors.append({"index": index, "message": "元数据格式错误；请检查标题、作者、日期和链接"})
        # Intentional partial import, made explicit in the preview and response.
        created = []
        if not dry_run:
            for row in prepared:
                try:
                    paper = self.create_paper(row["data"], owner=owner)
                    row["_reference"]["paper_id"] = paper["id"]
                    created.append(paper["id"])
                except HTTPException as exc:
                    if exc.status_code != 409:
                        raise
                    conflicts.append({"index": row["index"], "title": row["data"].title,
                                      "reason": "导入期间产生重复；没有覆盖现有记录",
                                      "matches": [], "recorded_work_ids": [],
                                      "recorded_snapshots": []})

            for conflict in conflicts:
                for match in conflict.get("matches", []):
                    ref = match.pop("_reference", None)
                    if ref and ref.get("paper_id"):
                        match["work_id"] = ref["paper_id"]
                        match.pop("item_index", None)
                    elif ref:
                        match.pop("work_id", None)

            for conflict in conflicts:
                refs = conflict.pop("_references", [])
                work_ids = list(dict.fromkeys(ref["paper_id"] for ref in refs if ref.get("paper_id")))
                if not work_ids:
                    continue
                timestamp = now()
                declaration = conflict.get("declaration", {})
                matched_work_ids = sorted(work_ids)
                snapshot_records = []
                with self.db.connect() as con:
                    for work_id in matched_work_ids:
                        rows = []
                        for kind, payload in (
                            ("identifier_declaration", {
                                "state": "requires_manual_review",
                                "declared_metadata": declaration,
                                "matched_work_ids": matched_work_ids,
                            }),
                            ("identity_conflict", {
                                "state": "requires_manual_review",
                                "reason": conflict["reason"],
                                "declared_metadata": declaration,
                                "matched_work_ids": matched_work_ids,
                                "matches": conflict["matches"],
                            }),
                        ):
                            encoded = encode_knowledge_snapshot(payload)
                            existing_snapshot = con.execute(
                                "SELECT id FROM source_snapshots WHERE owner=? AND work_id=? "
                                "AND snapshot_kind=? AND payload=? LIMIT 1",
                                (owner, work_id, kind, encoded)).fetchone()
                            if existing_snapshot:
                                snapshot_id = existing_snapshot["id"]
                            else:
                                snapshot_id = append_source_snapshot(
                                    con, work_id=work_id, owner=owner, kind=kind,
                                    payload=payload, retrieved_at=timestamp)
                            rows.append(snapshot_id)
                        snapshot_records.append({"work_id": work_id, "source_snapshot_ids": rows})
                conflict["recorded_work_ids"] = matched_work_ids
                conflict["recorded_snapshots"] = snapshot_records

        for conflict in conflicts:
            conflict.pop("_references", None)
            for match in conflict.get("matches", []):
                match.pop("_reference", None)
        return {"dry_run": dry_run, "ready": len(prepared), "skipped": skipped, "errors": errors,
                "preview": [row["data"].model_dump(mode="json") for row in prepared[:20]],
                "created": created, "conflicts": conflicts}

    def seed_demo(self, *, owner: str) -> int:
        # Seed only on explicit user action; all records and observations are fictional.
        if any(p["is_demo"] for p in self.list_papers(owner=owner)):
            return 0
        examples = [
            ("Layered Canvas: Editable Image Decomposition", ["图层分解 / 生成"], "baseline", ["code", "checkpoint", "dataset"], ["metadata_accessible", "gated", "indeterminate"]),
            ("Constraint-first Layout Generation", ["Layout 生成"], "reading", ["code", "evaluation"], ["metadata_accessible", "metadata_accessible"]),
            ("Scene Layers with Occlusion Awareness", ["图层分解 / 生成", "Layout 生成"], "inbox", ["code"], ["indeterminate"]),
            ("A Small Recipe for Sequential Recommendation", ["推荐系统"], "read", ["code", "dataset"], ["metadata_accessible", "metadata_accessible"]),
            ("Layout Editing through Local Constraints", ["Layout 生成"], "inbox", ["checkpoint"], [None]),
            ("Learning to Separate Visual Layers", ["图层分解 / 生成"], "reading", [], []),
        ]
        labels = {"code": "代码仓库", "checkpoint": "模型权重", "dataset": "数据集", "evaluation": "评测脚本"}
        for index, (title, topics, status, kinds, results) in enumerate(examples):
            paper = self.create_paper(PaperInput(title=title, authors=["re0 示例作者"], year=2026,
                                                venue="虚构演示 · 非真实论文", topics=topics, status=status,
                                                abstract="这是一条用于体验产品的虚构论文。展示研究方向、多维资源状态、证据记录与 baseline 筛选流程；不可用于学术引用。",
                                                notes="演示笔记：先检查输入输出和实验协议，再确认所需资源。此记录没有对应真实论文。",
                                                version_label="演示 v1"), owner=owner, demo=True)
            for n, (kind, result) in enumerate(zip(kinds, results)):
                resource = self.create_resource(paper["id"], ResourceInput(kind=kind, label=labels[kind],
                    url=f"https://example.org/re0-demo/{index}/{kind}",
                    ownership="unconfirmed"), owner=owner)
                if result:
                    summary = {"metadata_accessible": "演示：元数据与候选文件可定位；没有进行运行验证。",
                               "gated": "演示：模型页面存在，但文件访问需要申请。",
                               "indeterminate": "演示：本次未定位到所需资源，不能认定资源不存在。"}[result]
                    self.save_observation(resource["id"], {
                        "status": result, "summary": summary, "provider": "demo", "checked_at": now(),
                        "revision": "fictional-demo", "depth": "demo", "scope": "虚构演示数据，无任何外部检查",
                        "limitations": ["虚构的交互示例，不是对真实论文或资源的核验。"],
                        "indicators": {"training": ["train.py"], "inference": ["inference.py"]} if kind == "code" and result == "metadata_accessible" else {},
                        "evidence": [{"source_url": resource["url"], "locator": "演示说明", "excerpt": summary, "category": "demo"}],
                        "discovered": [], "license_id": "", "content_sha256": "", "paper_version_snapshot": "演示 v1"
                    }, owner=owner)
        return len(examples)

    def clear_demo(self, *, owner: str) -> int:
        with self.db.connect() as con:
            return con.execute("DELETE FROM papers WHERE is_demo=1 AND owner=?", (owner,)).rowcount

    def delete_owner_data(self, *, owner: str) -> int:
        """Delete every database row for one verified temporary owner, including evidence and settings.

        Owner-bearing tables are discovered from SQLite's schema and removed child-first by their
        foreign-key graph. Rows without an owner column (for example resources and observations)
        are removed through their parent's declared cascades. `local` and caller-provided strings are
        refused; this method is for the server's guest-retention cleanup only.
        """
        if not re.fullmatch(r"ws_[0-9a-f]{16}", str(owner or "")):
            raise ValueError("仅支持删除已验证的临时访客工作区")
        with self.db.connect() as con:
            tables = [row[0] for row in con.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
            owned = set()
            for table in tables:
                columns = {row[1] for row in con.execute(f'PRAGMA table_info("{table}")')}
                if "owner" in columns:
                    owned.add(table)

            children: dict[str, set[str]] = {table: set() for table in owned}
            for child in owned:
                for foreign_key in con.execute(f'PRAGMA foreign_key_list("{child}")'):
                    parent = foreign_key[2]
                    if parent in owned:
                        children[parent].add(child)
            order: list[str] = []
            visited: set[str] = set()

            def visit(table: str):
                if table in visited:
                    return
                visited.add(table)
                for child in sorted(children[table]):
                    visit(child)
                order.append(table)

            for table in sorted(owned):
                visit(table)

            con.execute("BEGIN IMMEDIATE")
            deleted = 0
            for table in order:
                safe_table = table.replace('"', '""')
                result = con.execute(f'DELETE FROM "{safe_table}" WHERE owner=?', (owner,))
                deleted += max(0, result.rowcount)
            return deleted


def bibtex_export(papers: list[dict]) -> str:
    def tex(value: str) -> str:
        table = {"\\": r"\textbackslash{}", "{": r"\{", "}": r"\}", "%": r"\%", "&": r"\&", "#": r"\#", "_": r"\_", "$": r"\$", "~": r"\textasciitilde{}", "^": r"\textasciicircum{}"}
        return "".join(table.get(c, c) for c in value.replace("\n", " "))
    result = []
    for p in papers:
        fields = {"title": p["title"], "author": " and ".join(p["authors"]), "year": str(p["year"] or ""), "doi": p["doi"], "url": p["paper_url"], "eprint": p["arxiv_id"]}
        if p["is_demo"]:
            fields["note"] = "FICTIONAL DEMO - NOT A REAL PUBLICATION"
        result.append("@misc{re0_" + p["id"].replace("-", "")[:12] + ",\n" + ",\n".join(f"  {key} = {{{tex(value)}}}" for key, value in fields.items() if value) + "\n}")
    return "\n\n".join(result) + "\n"
