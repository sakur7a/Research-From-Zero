"""Build an owner-scoped resource matrix from one completed Agent run.

The model may propose which checked resource belongs beside which source-derived paper. This
module keeps that proposal separate from the audit observation and labels it for human review.
The only importable paper data still comes from a paper tool result, and each selected proposal is
previewed before the library writes it.
"""
from __future__ import annotations

import re

from fastapi import HTTPException

from ..models import ResourceAudit
from ..resource_matrix import matrix as build_matrix
from ..resource_matrix import safe_link


def _work_identity(paper: dict) -> tuple[str, str]:
    identifier = str(paper.get("arxiv_id") or paper.get("doi") or "")
    version = str(paper.get("version_label") or "")
    arxiv_version = re.search(r"v\d+$", str(paper.get("arxiv_id") or ""))
    if arxiv_version and not version:
        version = arxiv_version.group()
    return identifier, version


def build_run_matrix(run: dict) -> dict:
    """Return JSON/Markdown/CSV data for the selected owner's run, without changing stored evidence."""
    evidence = run.get("evidence") or []
    by_id = {item.get("id"): item for item in evidence if isinstance(item, dict) and item.get("id")}
    documents: list[dict] = []
    papers: dict[str, dict] = {}
    for item in evidence:
        paper = item.get("paper") if isinstance(item, dict) else None
        if item.get("kind") != "paper" or not isinstance(paper, dict) or not paper.get("title"):
            continue
        evidence_id = item["id"]
        if evidence_id in papers:
            continue
        document = {"paper": paper, "resource_audits": [], "artifact_outcome": "not_checked",
                    "artifact_search_detail": {"reason_not_run":
                                                "本任务未提出论文关联；未关联检查另列，不表示资源不存在"}}
        papers[evidence_id] = document
        documents.append(document)

    metadata_by_paper: dict[str, list[dict]] = {evidence_id: [] for evidence_id in papers}
    approval_candidates = []
    linked_resource_ids: set[str] = set()
    relation_links = (run.get("report") or {}).get("resource_links") or []
    for relation in relation_links:
        paper_id = str(relation.get("paper_evidence_id") or "")
        resource_id = str(relation.get("resource_evidence_id") or "")
        paper_evidence, resource_evidence = by_id.get(paper_id), by_id.get(resource_id)
        document = papers.get(paper_id)
        audits = resource_evidence.get("resource_audits") if resource_evidence else None
        if (document is None or not paper_evidence or not resource_evidence
                or resource_evidence.get("kind") != "resource_check" or not isinstance(audits, list)):
            # Old/corrupt rows are visible as unlinked material below; they never become import data.
            continue
        paper = document["paper"]
        identifier, version = _work_identity(paper)
        relation_ids = list(relation.get("relation_evidence_ids") or [])
        sources = []
        for evidence_id in relation_ids:
            source = by_id.get(evidence_id)
            if not source:
                continue
            url = safe_link(source.get("source_url", ""))
            sources.append({"evidence_id": evidence_id, "source_url": url,
                            "locator": str(source.get("locator") or "")[:400],
                            "retrieved_at": str(source.get("retrieved_at") or "")})
        if not audits:
            continue
        linked_resource_ids.add(resource_id)
        candidate_audits = []
        for raw_audit in audits:
            try:
                audit = ResourceAudit.model_validate(raw_audit).model_dump(mode="json")
            except (TypeError, ValueError):
                continue
            audit["paper_title"] = str(paper.get("title") or audit.get("paper_title") or "")
            audit["work_identifier"] = identifier[:300]
            audit["work_version"] = version[:200]
            document["resource_audits"].append(audit)
            candidate_audits.append(audit)
            metadata_by_paper[paper_id].append({
                "association_status": "模型提出的候选关联；待人工确认",
                "paper_evidence_id": paper_id,
                "resource_evidence_id": resource_id,
                "association_evidence_ids": relation_ids,
                "association_sources": [
                    f"{item['evidence_id']} · {item['locator']} · {item['retrieved_at']} · {item['source_url']}"
                    for item in sources],
                "association_evidence": sources,
                "association_note": str(relation.get("rationale") or "")[:600],
            })
        if candidate_audits:
            approval_candidates.append({"paper_evidence_id": paper_id,
                                        "resource_evidence_id": resource_id,
                                        "paper": paper, "audits": candidate_audits,
                                        "relation_evidence_ids": relation_ids,
                                        "association_evidence": sources,
                                        "association_note": str(relation.get("rationale") or "")[:600]})

    row_metadata = []
    for evidence_id, document in papers.items():
        if document["resource_audits"]:
            row_metadata.extend(metadata_by_paper[evidence_id])
        else:
            row_metadata.append({"association_status": "本任务未关联已核验资源",
                                 "paper_evidence_id": evidence_id})
    unlinked_checks = []
    for item in evidence:
        if item.get("kind") != "resource_check" or item.get("id") in linked_resource_ids:
            continue
        for audit in item.get("resource_audits") or []:
            unlinked_checks.append({"resource_evidence_id": item.get("id", ""),
                                    "resource_url": safe_link(audit.get("resource_url", "")),
                                    "status": audit.get("status", "unknown"),
                                    "provider_status": audit.get("provider_status", ""),
                                    "checked_at": audit.get("checked_at", ""),
                                    "note": "资源已核验，但报告没有提出论文关联；未放入可保存矩阵。"})

    payload = build_matrix(documents, {"agent_run": {"run_id": run.get("id", ""),
                                                       "paper_evidence": len(papers),
                                                       "proposed_links": len(approval_candidates),
                                                       "unlinked_checks": len(unlinked_checks)}},
                          generated_at=str(run.get("updated_at") or ""),
                          row_metadata=row_metadata)
    payload.update({"source": "agent_run", "run_id": run.get("id", ""),
                    "approval_candidates": approval_candidates,
                    "unlinked_checks": unlinked_checks,
                    "association_note": "论文—资源关系由模型提出，只表示候选；提交预览和确认后才写入本人的文献库。"
                                       "官方归属、论文版本对应、可运行性仍按审计中的原始状态保留。"})
    return payload


def selected_import_items(payload: dict, selections: list[dict]) -> tuple[list[dict], list[list[dict]]]:
    """Resolve row selections against the run's stored report and source evidence.

    Return importable source records separately from the trusted association provenance. The latter
    is produced from this owner's stored run, never from a client-supplied confirmation payload.
    """
    wanted = {(str(item.get("paper_evidence_id") or ""),
               str(item.get("resource_evidence_id") or "")) for item in selections}
    candidates = {(item["paper_evidence_id"], item["resource_evidence_id"]): item
                  for item in payload.get("approval_candidates", [])}
    if not wanted or not wanted.issubset(candidates):
        raise HTTPException(404, "所选候选关联不属于这个任务，或任务中已没有对应证据")
    by_paper: dict[str, dict] = {}
    associations_by_paper: dict[str, list[dict]] = {}
    seen_audits: set[tuple] = set()
    for key in sorted(wanted):
        item = candidates[key]
        entry = by_paper.setdefault(item["paper_evidence_id"],
                                    {"paper": item["paper"], "audits": []})
        associations_by_paper.setdefault(item["paper_evidence_id"], []).append({
            "run_id": str(payload.get("run_id") or "")[:80],
            "paper_evidence_id": item["paper_evidence_id"],
            "resource_evidence_id": item["resource_evidence_id"],
            "resource_urls": sorted({str(audit.get("resource_url") or "")
                                      for audit in item["audits"] if audit.get("resource_url")}),
            "evidence_ids": list(item.get("relation_evidence_ids") or []),
            "sources": list(item.get("association_evidence") or []),
            "rationale": str(item.get("association_note") or "")[:600],
        })
        for audit in item["audits"]:
            checked = ResourceAudit.model_validate(audit).model_dump(mode="json")
            identity = (item["paper_evidence_id"], checked["resource_url"],
                        checked.get("checked_at", ""), checked.get("revision", ""),
                        checked.get("status", ""))
            if identity not in seen_audits:
                entry["audits"].append(checked)
                seen_audits.add(identity)
    items = list(by_paper.values())
    associations = [associations_by_paper[paper_evidence_id] for paper_evidence_id in by_paper]
    return items, associations
