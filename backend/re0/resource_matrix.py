"""The resource matrix: one row per resource candidate, in three shapes that cannot disagree.

A matrix is what turns six audits into an answer to "which of these can I actually use as a
baseline". It is only useful if a cell claiming a checkpoint exists can be opened, so every row
keeps the sources its conclusions rest on, and the JSON, Markdown and CSV exports are all rendered
from the one row list — three adapters over one structure, never three separate readings of it.

A paper with no candidate still gets a row. Dropping it would make "we searched and found nothing"
and "the search failed" both look like "this paper has no resources", which is the confusion the
audit states exist to prevent.

CSV is written defensively: a cell starting with `=`, `+`, `-`, `@`, a tab or a carriage return is
*executed* by spreadsheet software, and every string here arrived from another service.
"""
from __future__ import annotations

import csv
import io
import json
import pathlib
import time

from .models import AUDIT_COMPONENTS, AUDIT_LABELS, COMPONENT_LABELS, safe_url

MATRIX_SCHEMA_VERSION = "2"
# A comparison set wider than this stops being comparable; the matrix says so rather than hiding
# rows, because the reader chose the query that produced them.
COMPARABLE_PAPERS = 6
FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


def safe_link(url: str) -> str:
    """Only an http(s) link a reader can open. Anything else becomes empty rather than being
    written into a report as though it were a source."""
    try:
        return safe_url(url or "")
    except ValueError:
        return ""


def csv_cell(value) -> str:
    """One cell, neutralised for a spreadsheet.

    A leading `=` is a formula, and `-2` or `@SUM(...)` are read as one too. Prefixing a quote is
    the standard defence and leaves the value readable in every other consumer.
    """
    text = "" if value is None else str(value)
    if text.startswith(FORMULA_PREFIXES):
        text = "'" + text
    return text.replace("\r", " ").replace("\n", " ")


def markdown_cell(value) -> str:
    text = "" if value is None else str(value)
    return text.replace("|", "\\|").replace("\r", " ").replace("\n", " ")


def blockers(row: dict) -> list:
    """Why this row cannot be taken as a drop-in baseline, derived from what the audit found.

    Every item here is a restatement of a field on the row, never a new judgement: an unchecked
    candidate, a gate, an adapter that needs a base model, a version correspondence nobody
    established.
    """
    found = []
    if row["status"] == "not_checked":
        found.append("未核验（状态是候选，不是结论）")
    if row["status"] == "access_required":
        found.append("需申请访问")
    elif row["status"] == "access_failed":
        found.append("本次访问失败，可用性未知")
    elif row["status"] == "partially_available":
        found.append("仅部分可用")
    elif row["status"] == "unsupported":
        found.append("链接类型不受支持，未检查")
    if row["access"] == "requires_application":
        found.append("资源需申请")
    if any("adapter" in text for text in row["limitations"]):
        found.append("疑似 adapter/LoRA 权重，需要对应基础模型")
    if row["version_match"] == "unknown":
        found.append("论文版本与资源版本的对应关系未确认")
    if row["attribution"] == "unconfirmed":
        found.append("官方归属未确认（名称匹配或作者自述，未交叉验证）")
    return found


def audit_row(row: dict, document: dict) -> dict:
    """One audited candidate as one matrix row."""
    paper = document.get("paper") or {}
    coverage = {name: COMPONENT_LABELS[(row.get("coverage") or {}).get(name, {}).get("state", "unknown")]
                for name in AUDIT_COMPONENTS}
    # The pinned listing first: it is the one link that shows the whole scope the check read, so it
    # is what a reader opens to verify an absence. Component files follow.
    listing = next((safe_link(item.get("source_url", "")) for item in (row.get("evidence") or [])
                    if item.get("category") == "provider_metadata" and safe_link(item.get("source_url", ""))), "")
    component_sources = sorted({source for finding in (row.get("coverage") or {}).values()
                                for source in finding.get("sources") or []})
    links = ([listing] if listing else []) + [safe_link(item) for item in component_sources]
    links = [item for item in dict.fromkeys(links) if item]
    flat = {
        "paper_title": paper.get("title", "") or row.get("paper_title", ""),
        "work_identifier": row.get("work_identifier", ""),
        "work_version": row.get("work_version", ""),
        "resource_url": safe_link(row.get("resource_url", "")),
        "resource_type": row.get("resource_type", "unknown"),
        "candidate_origin": row.get("candidate_origin", ""),
        "attribution": row.get("attribution", "unconfirmed"),
        "author_declaration": row.get("author_declaration", "undeclared"),
        "status": row.get("status", "not_checked"),
        "status_label": AUDIT_LABELS.get(row.get("status", "not_checked"), row.get("status", "")),
        "provider_status": row.get("provider_status", ""),
        "access": row.get("access", "unknown"),
        "revision": row.get("revision", ""),
        "checked_at": row.get("checked_at", ""),
        "verification_depth": row.get("verification_depth", "not_checked"),
        "version_match": row.get("version_match", "unknown"),
        "licences": row.get("licences") or {},
        "coverage": coverage,
        "sources": [item for item in links if item][:8],
        "limitations": list(row.get("limitations") or []),
        "record_kind": "observation",
        "association_status": row.get("association_status", ""),
        "paper_evidence_id": row.get("paper_evidence_id", ""),
        "resource_evidence_id": row.get("resource_evidence_id", ""),
        "association_evidence_ids": list(row.get("association_evidence_ids") or []),
        "association_sources": list(row.get("association_sources") or []),
        "association_note": row.get("association_note", ""),
        "association_evidence": list(row.get("association_evidence") or []),
    }
    flat["blockers"] = blockers(flat)
    return flat


def absent_row(document: dict) -> dict:
    """A paper with no candidate row. Its state is the search's own outcome, so a failed search is
    not flattened into "no resources"."""
    paper = document.get("paper") or {}
    detail = document.get("artifact_search_detail") or {}
    state = document.get("artifact_outcome") or "not_checked"
    row = {
        "paper_title": paper.get("title", ""),
        "work_identifier": str(paper.get("doi") or paper.get("arxiv_id") or ""),
        "work_version": "",
        "resource_url": "",
        "resource_type": "unknown",
        "candidate_origin": "",
        "attribution": "unconfirmed",
        "author_declaration": "undeclared",
        "status": state,
        "status_label": AUDIT_LABELS.get(state, state),
        "provider_status": "",
        "access": "not_applicable" if state == "not_found_in_scope" else "unknown",
        "revision": "",
        "checked_at": "",
        "verification_depth": "not_checked",
        "version_match": "unknown",
        "licences": {},
        "coverage": {name: COMPONENT_LABELS["unknown"] for name in AUDIT_COMPONENTS},
        "sources": [],
        "limitations": list(detail.get("failures") or []),
        "record_kind": "observation",
        "association_status": "",
        "paper_evidence_id": "",
        "resource_evidence_id": "",
        "association_evidence_ids": [],
        "association_sources": [],
        "association_note": "",
        "association_evidence": [],
    }
    row["blockers"] = ["没有可审计的资源候选：" + {
        "not_found_in_scope": "名称检索完成且未命中，检查范围内未找到",
        "access_failed": "名称检索未完成，可用性未知",
        "not_checked": "本次未检索（未开启、预算已尽或标题没有可检索的项目名）",
    }.get(state, state)]
    if detail.get("reason_not_run"):
        row["blockers"].append("本次未检索原因：" + str(detail["reason_not_run"])[:600])
    return row


def rows(documents: list) -> list:
    found = []
    for document in documents:
        audits = document.get("resource_audits") or []
        found.extend(audit_row(row, document) for row in audits)
        if not audits:
            found.append(absent_row(document))
    return found


COLUMNS = ("paper_title", "work_identifier", "work_version", "resource_url", "resource_type",
           "candidate_origin", "attribution", "author_declaration", "status", "status_label",
           "provider_status", "access", "verification_depth", "version_match", "revision",
           "checked_at", "coverage", "licences", "sources", "blockers", "limitations",
           "record_kind", "association_status", "paper_evidence_id", "resource_evidence_id",
           "association_evidence_ids", "association_sources", "association_note")


def approval_items(documents: list) -> list:
    """The papers and their audit rows, in the shape the library's approval endpoint takes.

    Kept apart from the matrix rows on purpose. A matrix row is flattened for reading and has
    dropped the authors, the abstract and the identifiers; an approval has to carry the
    source-derived paper metadata untouched, or importing it would invent a record.
    """
    items = []
    for document in documents:
        audits = document.get("resource_audits") or []
        paper = document.get("paper") or {}
        if not audits or not paper.get("title"):
            # Nothing audited, or nothing to attribute it to. Importing either would create a
            # library record that no source supports.
            continue
        items.append({"paper": paper, "audits": audits})
    return items


def matrix(documents: list, coverage: dict, *, generated_at: str = "",
           row_metadata: list[dict] | None = None) -> dict:
    """The matrix as one structure. The Markdown and CSV exports are renderings of it."""
    found = rows(documents)
    metadata_fields = {"association_status", "paper_evidence_id", "resource_evidence_id",
                       "association_evidence_ids", "association_sources", "association_note",
                       "association_evidence"}
    for index, row in enumerate(found):
        if index < len(row_metadata or []):
            row.update({key: value for key, value in row_metadata[index].items()
                        if key in metadata_fields})
    titles = {row["paper_title"] for row in found}
    return {
        "schema_version": MATRIX_SCHEMA_VERSION,
        "generated_at": generated_at or time.strftime("%Y-%m-%dT%H:%M:%S"),
        "papers": len(titles),
        "rows": found,
        "coverage": coverage,
        "approval": approval_items(documents),
        "comparable": len(titles) <= COMPARABLE_PAPERS,
        "note": ("每行是一个资源候选的审计观察（record_kind=observation）；人工修订是另一种记录，"
                 "不会覆盖这一行。状态描述的是本次检查，不是资源是否存在。"
                 "approval 段是同一批结果的可导入载荷，POST /api/import/resource-audits 接受它，"
                 "默认只预览。"
                 + ("" if len(titles) <= COMPARABLE_PAPERS else
                    f" 本次覆盖 {len(titles)} 篇，超出 {COMPARABLE_PAPERS} 篇的比较集范围，"
                    "横向对比请先收窄查询。")),
    }


def markdown(payload: dict) -> str:
    lines = [f"# 资源矩阵（schema {payload['schema_version']}，生成于 {payload['generated_at']}）", ""]
    coverage = payload.get("coverage") or {}
    search = coverage.get("name_search") or {}
    candidates = coverage.get("candidates") or {}
    if search:
        states = search.get("states") or {}
        lines += [
            f"- 论文 {search.get('denominator', 0)} 篇；名称检索：完成 {states.get('searched', 0)}、"
            f"部分 {states.get('partial', 0)}、失败 {states.get('failed', 0)}、"
            f"无项目名 {states.get('skipped', 0)}、未检索 {states.get('not-run', 0)}",
            f"- 候选 {candidates.get('found', 0)} 个（作者自述 {candidates.get('declared', 0)}、"
            f"名称匹配 {candidates.get('name_matched', 0)}）；已核验 {candidates.get('verified', 0)}、"
            f"未核验 {candidates.get('unchecked', 0)}",
            "",
        ]
    lines += [payload["note"], "",
              "**这张表能回答**：某篇论文有没有可定位的资源候选、检查到了什么深度、哪些类别有候选文件、"
              "来源声明了什么许可证、以及为什么某一行还不能直接当 baseline 用。",
              "**这张表不能回答**：资源是否能跑通、结果能否复现、许可证是否允许你的用途、"
              "以及候选仓库是否真出自论文作者。这些都需要人工确认，确认后是另一条记录。", ""]
    headers = ("论文", "资源", "类型", "候选来源", "状态", "提供商状态", "访问", "核验深度",
               "归属", "作者声明", "版本对应", "revision", "检查时间", "许可证", "类别覆盖",
               "不可直接比较的条件", "可跳转来源", "候选关联",
               "关联证据 ID", "关联来源与时间", "关联说明")
    lines.append("| " + " | ".join(headers) + " |")
    lines.append("|" + "---|" * len(headers))
    for row in payload["rows"]:
        coverage_text = "; ".join(f"{name}={state}" for name, state in row["coverage"].items()
                                  if state != COMPONENT_LABELS["unknown"]) or "全部未知"
        licences = ", ".join(f"{name}={value}" for name, value in row["licences"].items()) or "未声明"
        sources = " ".join(row["sources"][:3]) or "（无）"
        cells = (row["paper_title"][:80], row["resource_url"] or "（无候选）", row["resource_type"],
                 row["candidate_origin"] or "（无）",
                 f"{row['status_label']} ({row['status']})", row.get("provider_status", ""), row["access"],
                 row["verification_depth"], row.get("attribution", "unconfirmed"),
                 row.get("author_declaration", "undeclared"), row["version_match"],
                 row.get("revision", ""), row.get("checked_at", ""), licences, coverage_text,
                 "; ".join(row["blockers"]) or "（无）", sources,
                 row.get("association_status", ""),
                 "; ".join(row.get("association_evidence_ids") or []),
                 "; ".join(row.get("association_sources") or []),
                 row.get("association_note", ""))
        lines.append("| " + " | ".join(markdown_cell(cell) for cell in cells) + " |")
    lines += ["", "## 限制", ""]
    limits = sorted({text for row in payload["rows"] for text in row["limitations"]})
    lines += [f"- {markdown_cell(text)}" for text in limits[:20]] or ["- 本次没有记录到限制说明。"]
    return "\n".join(lines) + "\n"


def csv_text(payload: dict) -> str:
    """A header row plus one row per candidate. `csv` handles quoting; `csv_cell` handles the
    formula injection quoting does not cover."""
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow([csv_cell(name) for name in COLUMNS])
    for row in payload["rows"]:
        cells = []
        for name in COLUMNS:
            value = row.get(name)
            if isinstance(value, dict):
                value = "; ".join(f"{key}={item}" for key, item in value.items())
            elif isinstance(value, list):
                value = "; ".join(str(item) for item in value)
            cells.append(csv_cell(value))
        writer.writerow(cells)
    return buffer.getvalue()


def write(prefix, documents: list, coverage: dict, *, generated_at: str = "") -> list:
    """Write `.json`, `.md` and `.csv` beside `prefix` and return the three paths.

    Each file is written beside its destination and moved into place, so an interrupted write
    cannot leave a half matrix that later reads as a complete one.
    """
    base = pathlib.Path(prefix).expanduser()
    if base.suffix:
        base = base.with_suffix("")
    if str(base.parent) not in {"", "."} and not base.parent.is_dir():
        base.parent.mkdir(parents=True, exist_ok=True)
    payload = matrix(documents, coverage, generated_at=generated_at)
    bodies = ((".json", json.dumps(payload, ensure_ascii=False, indent=2)),
              (".md", markdown(payload)), (".csv", csv_text(payload)))
    written = []
    for suffix, body in bodies:
        path = base.with_suffix(suffix)
        temporary = path.with_suffix(suffix + ".tmp")
        temporary.write_text(body, encoding="utf-8")
        temporary.replace(path)
        written.append(path)
    return written
