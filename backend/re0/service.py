"""Application services: explicit identities, safe imports, immutable evidence."""
from __future__ import annotations

import json
import re
import sqlite3
from uuid import uuid4

from fastapi import HTTPException
from pydantic import ValidationError

from .db import Database, encode
from .models import PaperInput, ResourceInput, now


def paper_record(row) -> dict:
    return {**json.loads(row["data"]), "id": row["id"], "created_at": row["created_at"],
            "updated_at": row["updated_at"], "is_demo": bool(row["is_demo"])}


def resource_record(row) -> dict:
    return {**json.loads(row["data"]), "id": row["id"], "paper_id": row["paper_id"], "created_at": row["created_at"]}


class Store:
    def __init__(self, db: Database):
        self.db = db

    def list_papers(self) -> list[dict]:
        with self.db.connect() as con:
            papers = [paper_record(row) for row in con.execute("SELECT * FROM papers ORDER BY updated_at DESC, id")]
            resources = [resource_record(row) for row in con.execute("SELECT * FROM resources ORDER BY created_at, id")]
            rows = con.execute("""SELECT o.* FROM observations o JOIN
                (SELECT resource_id, MAX(id) mid FROM observations GROUP BY resource_id) latest ON latest.mid=o.id""")
            latest = {row["resource_id"]: {**json.loads(row["data"]), "id": row["id"]} for row in rows}
            by_paper: dict[str, list] = {}
            for resource in resources:
                resource["latest"] = latest.get(resource["id"])
                by_paper.setdefault(resource["paper_id"], []).append(resource)
            for paper in papers:
                paper["resources"] = by_paper.get(paper["id"], [])
            return papers

    def get_paper(self, paper_id: str) -> dict:
        for paper in self.list_papers():
            if paper["id"] == paper_id:
                return paper
        raise HTTPException(404, "论文不存在")

    def create_paper(self, data: PaperInput, *, demo: bool = False) -> dict:
        paper_id, timestamp = str(uuid4()), now()
        try:
            with self.db.connect() as con:
                con.execute("INSERT INTO papers VALUES (?,?,?,?,?,?,?)", (
                    paper_id, encode(data.model_dump(mode="json")), data.doi,
                    re.sub(r"v\d+$", "", data.arxiv_id), timestamp, timestamp, int(demo)))
                con.executemany("INSERT OR IGNORE INTO topics VALUES (?)", [(name,) for name in data.topics])
        except sqlite3.IntegrityError as exc:
            raise HTTPException(409, "此 DOI 或 arXiv 已在文献库中；请编辑已有条目，原有笔记不会被覆盖") from exc
        return self.get_paper(paper_id)

    def update_paper(self, paper_id: str, data: PaperInput) -> dict:
        try:
            with self.db.connect() as con:
                result = con.execute("UPDATE papers SET data=?,doi=?,arxiv_base=?,updated_at=? WHERE id=?", (
                    encode(data.model_dump(mode="json")), data.doi, re.sub(r"v\d+$", "", data.arxiv_id), now(), paper_id))
                if not result.rowcount:
                    raise HTTPException(404, "论文不存在")
                con.executemany("INSERT OR IGNORE INTO topics VALUES (?)", [(name,) for name in data.topics])
        except sqlite3.IntegrityError as exc:
            raise HTTPException(409, "此 DOI 或 arXiv 已被另一篇论文使用；没有覆盖任何条目") from exc
        return self.get_paper(paper_id)

    def delete_paper(self, paper_id: str):
        with self.db.connect() as con:
            if not con.execute("DELETE FROM papers WHERE id=?", (paper_id,)).rowcount:
                raise HTTPException(404, "论文不存在")

    def create_resource(self, paper_id: str, data: ResourceInput) -> dict:
        resource_id = str(uuid4())
        with self.db.connect() as con:
            if not con.execute("SELECT 1 FROM papers WHERE id=?", (paper_id,)).fetchone():
                raise HTTPException(404, "论文不存在")
            con.execute("INSERT INTO resources VALUES (?,?,?,?)", (resource_id, paper_id, encode(data.model_dump(mode="json")), now()))
            con.execute("UPDATE papers SET updated_at=? WHERE id=?", (now(), paper_id))
        return self.get_resource(resource_id)

    def get_resource(self, resource_id: str) -> dict:
        with self.db.connect() as con:
            row = con.execute("SELECT * FROM resources WHERE id=?", (resource_id,)).fetchone()
            if not row:
                raise HTTPException(404, "资源不存在")
            return resource_record(row)

    def history(self, resource_id: str) -> list[dict]:
        self.get_resource(resource_id)
        with self.db.connect() as con:
            return [{**json.loads(row["data"]), "id": row["id"]} for row in con.execute(
                "SELECT * FROM observations WHERE resource_id=? ORDER BY id DESC", (resource_id,))]

    def save_observation(self, resource_id: str, observation: dict):
        try:
            with self.db.connect() as con:
                con.execute("INSERT INTO observations(resource_id,data,checked_at) VALUES (?,?,?)",
                            (resource_id, encode(observation), observation["checked_at"]))
        except sqlite3.IntegrityError as exc:
            raise HTTPException(409, "检查期间资源已被删除，结果未写入其他资源") from exc
        return self.history(resource_id)[0]

    def delete_resource(self, resource_id: str):
        with self.db.connect() as con:
            if not con.execute("DELETE FROM resources WHERE id=?", (resource_id,)).rowcount:
                raise HTTPException(404, "资源不存在")

    def topics(self) -> list[str]:
        with self.db.connect() as con:
            return [row[0] for row in con.execute("SELECT name FROM topics ORDER BY name")]

    def add_topic(self, name: str):
        with self.db.connect() as con:
            con.execute("INSERT OR IGNORE INTO topics VALUES (?)", (name,))

    def export(self) -> dict:
        papers = self.list_papers()
        for paper in papers:
            for resource in paper["resources"]:
                resource["observations"] = self.history(resource["id"])
        return {"schema_version": 1, "exported_at": now(), "topics": self.topics(), "papers": papers}

    def import_csl(self, items: list[dict], dry_run: bool = True) -> dict:
        if len(items) > 500:
            raise HTTPException(422, "单次最多导入 500 条 CSL JSON 记录")
        existing = self.list_papers()
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
                    created.append(self.create_paper(data)["id"])
                except HTTPException as exc:
                    if exc.status_code != 409:
                        raise
                    conflicts.append({"title": data.title, "message": "导入期间产生重复，已跳过"})
        return {"dry_run": dry_run, "ready": len(prepared), "skipped": skipped, "errors": errors,
                "preview": [x.model_dump(mode="json") for x in prepared[:20]], "created": created, "conflicts": conflicts}

    def seed_demo(self) -> int:
        # Seed only on explicit user action; all records and observations are fictional.
        if any(p["is_demo"] for p in self.list_papers()):
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
                                                version_label="演示 v1"), demo=True)
            for n, (kind, result) in enumerate(zip(kinds, results)):
                resource = self.create_resource(paper["id"], ResourceInput(kind=kind, label=labels[kind],
                    url=f"https://example.org/re0-demo/{index}/{kind}",
                    ownership="unconfirmed"))
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
                    })
        return len(examples)

    def clear_demo(self) -> int:
        with self.db.connect() as con:
            return con.execute("DELETE FROM papers WHERE is_demo=1").rowcount


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
