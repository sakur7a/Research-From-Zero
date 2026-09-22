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
from .models import PaperInput, ResourceAudit, ResourceInput, ResourceKind, now

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
        except sqlite3.IntegrityError as exc:
            raise HTTPException(409, "此 DOI 或 arXiv 已在文献库中；请编辑已有条目，原有笔记不会被覆盖") from exc
        return self.get_paper(paper_id, owner=owner)

    def update_paper(self, paper_id: str, data: PaperInput, *, owner: str) -> dict:
        try:
            with self.db.connect() as con:
                result = con.execute("UPDATE papers SET data=?,doi=?,arxiv_base=?,updated_at=? "
                                     "WHERE id=? AND owner=?", (
                                         encode(data.model_dump(mode="json")), data.doi,
                                         re.sub(r"v\d+$", "", data.arxiv_id), now(), paper_id, owner))
                if not result.rowcount:
                    raise HTTPException(404, "论文不存在")
                con.executemany("INSERT OR IGNORE INTO topics(name,owner) VALUES (?,?)",
                                [(name, owner) for name in data.topics])
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
        # Ownership is checked before the write: an observation appended to somebody else's resource
        # cannot be un-appended, and the foreign key would not stop it.
        self.get_resource(resource_id, owner=owner)
        record = {**observation, "record_kind": kind, "record_origin": origin, "recorded_at": now()}
        try:
            with self.db.connect() as con:
                con.execute("INSERT INTO observations(resource_id,data,checked_at) VALUES (?,?,?)",
                            (resource_id, encode(record), record.get("checked_at") or now()))
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

    def import_audits(self, items: list[dict], dry_run: bool = True, *, owner: str) -> dict:
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
                    self.save_observation(resource_id, audit.model_dump(mode="json"), owner=owner,
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

    def export(self, *, owner: str) -> dict:
        papers = self.list_papers(owner=owner)
        for paper in papers:
            for resource in paper["resources"]:
                resource["observations"] = self.history(resource["id"], owner=owner)
        # `owner` is in the export because a file that does not say whose library it is can be
        # imported into somebody else's, and then the answer is wrong in a way nobody can see.
        return {"schema_version": 2, "exported_at": now(), "owner": owner,
                "topics": self.topics(owner=owner), "papers": papers}

    def import_csl(self, items: list[dict], dry_run: bool = True, *, owner: str) -> dict:
        if len(items) > 500:
            raise HTTPException(422, "单次最多导入 500 条 CSL JSON 记录")
        existing = self.list_papers(owner=owner)
        dois = {p["doi"] for p in existing if p["doi"]}
        arxivs = {re.sub(r"v\d+$", "", p["arxiv_id"]) for p in existing if p["arxiv_id"]}
        titles = {p["title"].strip().casefold() for p in existing}
        prepared, skipped, errors = [], [], []
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
                arxiv_base = re.sub(r"v\d+$", "", data.arxiv_id)
                if (data.doi and data.doi in dois) or (arxiv_base and arxiv_base in arxivs) or data.title.casefold() in titles:
                    skipped.append({"index": index, "title": data.title, "reason": "ID 重复或标题相同（保守跳过，不覆盖）"})
                    continue
                dois.add(data.doi)
                arxivs.add(arxiv_base)
                titles.add(data.title.casefold())
                prepared.append(data)
            except (ValidationError, ValueError, TypeError, KeyError, IndexError, AttributeError):
                errors.append({"index": index, "message": "元数据格式错误；请检查标题、作者、日期和链接"})
        # Intentional partial import, made explicit in the preview and response.
        created, conflicts = [], []
        if not dry_run:
            for data in prepared:
                try:
                    created.append(self.create_paper(data, owner=owner)["id"])
                except HTTPException as exc:
                    if exc.status_code != 409:
                        raise
                    conflicts.append({"title": data.title, "message": "导入期间产生重复，已跳过"})
        return {"dry_run": dry_run, "ready": len(prepared), "skipped": skipped, "errors": errors,
                "preview": [x.model_dump(mode="json") for x in prepared[:20]], "created": created, "conflicts": conflicts}

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
