"""Regenerate tests/fixtures/search-result.json through the real code path.

The retrieval workbench (`web/search.html`) is tested in Node against that file, and a Python test
calls `build()` here and compares, so the fixture cannot quietly drift from what
`re0 paper search --json` actually writes: it goes through `paper_document`, the real `ResourceAudit`
model, `run_coverage` and `result_model.normalize`.

Every paper, DOI, arXiv id, repository and licence in it is **fictional**, and the payload says so in
its own `note` field, which the page renders. Regenerate with:

    python scripts/gen_search_fixture.py
"""
from __future__ import annotations

import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from re0 import resource_audit, result_model
from re0.agent.tools import paper_document
from re0.models import ComponentFinding, Evidence, PaperInput, ResourceAudit

OUT = ROOT / "tests" / "fixtures" / "search-result.json"


def record(title, authors, year, venue, doi, arxiv, url, abstract, sources, citations,
           publication, institutions, queries, preprint_also=False):
    paper = PaperInput(title=title, authors=authors, year=year, venue=venue, abstract=abstract,
                       doi=doi, arxiv_id=arxiv, paper_url=url)
    return {"paper": paper, "sources": sources, "citations": citations, "venue": venue,
            "publication": publication, "institutions": institutions, "queries": queries,
            "preprint_also": preprint_also}


def finding(state, sources=(), paths=()):
    return ComponentFinding(state=state, sources=list(sources), paths=list(paths))


def source_payload() -> dict:
    """The single fake tool result consumed by CLI JSON, MCP, and the Web workbench."""
    documents = []

    # 1. A conference paper with an official repository that was actually read.
    first = paper_document(record(
        "LayerKit: Layered Scene Generation with Decomposed Diffusion",
        ["Wei Chen", "Amara Osei", "Liu Yang"], 2025, "虚构演示会议",
        "10.99999/fixture-layerkit", "2412.99991",
        "https://doi.org/10.99999/fixture-layerkit",
        "We present LayerKit, a layered scene generation model. Code and checkpoints are available at "
        "https://github.com/example-lab/layerkit.",
        ["crossref", "openalex"], 128,
        {"state": "venue", "venue": "虚构演示会议 · 非真实论文集",
         "source": "crossref"},
        ["Shenzhen University", "Example Lab"], ["layer decomposition", "layered generation"]))
    first["resource_audits"] = [ResourceAudit(
        paper_title=first["paper"]["title"],
        work_identifier="10.99999/fixture-layerkit", work_version="v2",
        resource_url="https://github.com/example-lab/layerkit", resource_type="code",
        candidate_origin="摘要中自述", attribution="official",
        attribution_evidence=[Evidence(source_url="https://doi.org/10.99999/fixture-layerkit",
                                       excerpt="Code and checkpoints are available at https://github.com/example-lab/layerkit")],
        author_declaration="released",
        author_declaration_evidence=[Evidence(source_url="https://arxiv.org/abs/2412.99991",
                                              excerpt="Code and checkpoints are available at https://github.com/example-lab/layerkit")],
        status="metadata_readable", provider="github",
        provider_status="200", summary="仓库存在，README 描述了训练与推理入口", access="open",
        scope="仓库根目录与 README", revision="9f3c1ab", checked_at="2026-09-22T09:14:00Z",
        verification_depth="file_listing",
        coverage={"code_training": finding("present", ["https://github.com/example-lab/layerkit/tree/main/train"], ["train/layerkit.py"]),
                  "code_inference": finding("present", ["https://github.com/example-lab/layerkit/tree/main/demo"], ["demo.py"]),
                  "checkpoint": finding("requires_access", ["https://huggingface.co/example-lab/layerkit"]),
                  "dataset": finding("absent_in_scope", ["https://github.com/example-lab/layerkit/tree/main"], []),
                  "environment": finding("present", ["https://github.com/example-lab/layerkit/blob/main/environment.yml"], ["environment.yml"])},
        evidence=[Evidence(source_url="https://api.github.com/repos/example-lab/layerkit/contents",
                           locator="listing", excerpt="train/ demo/ environment.yml README.md",
                           category="provider_metadata")],
        licences={"code": "MIT", "checkpoint": "cc-by-nc-4.0"},
        version_match="matched", version_evidence="README 的 v2 说明与论文 arXiv v2 对应",
        limitations=["checkpoint 需要单独申请，仓库内只有下载脚本"],
    ).model_dump(mode="json")]
    first["artifact_search"] = "searched"
    first["artifact_search_detail"] = {"project_name": "layerkit"}
    first["artifact_outcome"] = "found"
    documents.append(first)

    # 2. A preprint whose only candidate is a third-party mirror that needs an application.
    second = paper_document(record(
        "LayoutDiff: Controllable Layout Generation", ["M. Rossi"], 2024, "arXiv.org",
        "", "2401.99992", "https://arxiv.org/abs/2401.99992",
        "A layout generation model. We plan to release code.",
        ["arxiv", "semanticscholar"], 41,
        {"state": "preprint", "venue": "arXiv.org", "source": "arxiv"},
        [], ["layered generation"], preprint_also=False))
    second["resource_audits"] = [ResourceAudit(
        paper_title=second["paper"]["title"], work_identifier="arXiv:2401.99992",
        resource_url="https://huggingface.co/papers/2401.99992", resource_type="checkpoint",
        candidate_origin="名称匹配", attribution="unconfirmed", author_declaration="promised",
        author_declaration_evidence=[Evidence(source_url="https://arxiv.org/abs/2401.99992",
                                              excerpt="We plan to release code.")],
        status="access_required", provider="huggingface", provider_status="401",
        summary="页面存在，权重需要同意条款后下载", access="requires_application",
        scope="模型卡片", checked_at="2026-09-22T09:15:00Z", verification_depth="metadata_only",
        coverage={"checkpoint": finding("requires_access", ["https://huggingface.co/papers/2401.99992"]),
                  "code_training": finding("unknown")},
        evidence=[Evidence(source_url="https://huggingface.co/api/models/papers-2401.99992",
                           locator="model card", excerpt="gated: true", category="provider_metadata")],
        version_match="unknown",
        limitations=["疑似 adapter 权重，需要对应基础模型", "名称匹配未经作者确认"],
    ).model_dump(mode="json")]
    second["artifact_search"] = "searched"
    second["artifact_search_detail"] = {"project_name": "layoutdiff"}
    second["artifact_outcome"] = "found"
    # A field this schema version does not describe: it must ride along, not vanish.
    second["future_provider_field"] = "carried, not understood"
    documents.append(second)

    # 3. A paper with no candidate at all, where the name search completed and found nothing.
    third = paper_document(record(
        "A Survey of Layered Image Synthesis", ["K. Nakamura", "P. Silva"], 2023, "虚构演示期刊 · 非真实论文",
        "10.99999/fixture-survey", "", "https://doi.org/10.99999/fixture-survey",
        "We survey layered image synthesis methods.",
        ["crossref", "openalex"], 302,
        {"state": "venue", "venue": "虚构演示期刊 · 非真实论文", "source": "openalex"},
        ["Some University"], ["layer decomposition"]))
    third["artifact_search"] = "searched"
    third["artifact_search_detail"] = {"project_name": ""}
    third["artifact_outcome"] = "not_found_in_scope"
    documents.append(third)

    # 4. A paper whose name search failed, so its absence says nothing about the resource.
    fourth = paper_document(record(
        "Depth-Aware Layer Compositing", ["A. Haddad"], 2026, "", "", "2602.99993",
        "https://arxiv.org/abs/2602.99993", "Compositing with depth ordering.",
        ["arxiv"], None, {"state": "unknown", "venue": "", "source": ""}, [], ["layered generation"]))
    fourth["artifact_search"] = "failed"
    fourth["artifact_search_detail"] = {"project_name": "depth-aware-layer-compositing",
                                        "failures": ["GitHub 搜索返回 403（限流）"]}
    fourth["artifact_outcome"] = "access_failed"
    documents.append(fourth)

    budget = resource_audit.Budget(find=6, verify=4)
    budget.find_left, budget.verify_left = 3, 2
    payload = {
        "documents": documents,
        "note": ("测试夹具：论文、作者、DOI、arXiv 编号、仓库与许可证全部是虚构的，仅用于页面与协议测试。"
                 "不要引用、不要当成真实检索结果，也不要导入文献库。"),
        "scope": "跨源书目匹配；同源与跨源重复项已合并；未阅读全文",
        "query": "layer decomposition",
        "queries": ["layer decomposition", "layered generation"],
        "sources_queried": ["arxiv", "crossref", "openalex", "semanticscholar"],
        "source_counts": {"arxiv": 2, "crossref": 2, "openalex": 2, "semanticscholar": 0},
        "source_failures": [{"source": "semanticscholar",
                             "error": "HTTP 429: 请求过于频繁（匿名调用被硬限流）"}],
        "incomplete_results": True,
        "duplicates_merged": 2,
        "dropped_out_of_range": 5,
        # The tool's own coverage block: the normalized structure now carries it through, so the file and
        # the MCP surface can state the window that was actually requested and what each attempt did.
        "coverage": {
            "requested": {"queries": ["layer decomposition", "layered generation"],
                          "sources": ["arxiv", "crossref", "openalex", "semanticscholar"],
                          "limit": 10, "start_year": 2023, "end_year": None, "max_pages": 2},
            "attempts": [{"query": "layer decomposition", "source": "crossref", "ok": True,
                          "effective_query": "layer decomposition", "records": 2},
                         {"query": "layered generation", "source": "arxiv", "ok": True,
                          "effective_query": "layered generation", "records": 2},
                         {"query": "layer decomposition", "source": "semanticscholar", "ok": False,
                          "effective_query": "layer decomposition",
                          "error": "HTTP 429: 请求过于频繁（匿名调用被硬限流）"}],
            "succeeded": ["arxiv", "crossref", "openalex"],
            "failed": [{"source": "semanticscholar",
                        "error": "HTTP 429: 请求过于频繁（匿名调用被硬限流）"}],
            "hits": {"records": 6, "unique": 4, "duplicates_merged": 2, "dropped_out_of_range": 5,
                     "unknown_year": 1},
            "state": "partial",
            "pagination": [{"source": "openalex", "query": "layer decomposition",
                            "effective_query": "layer decomposition", "pagination_documented": True,
                            "pages_fetched": 1, "requests_used": 2, "records": 2, "next_cursor": "",
                            "truncated": False, "stop_reason": "complete", "detail": ""},
                           {"source": "semanticscholar", "query": "layer decomposition",
                            "effective_query": "layer decomposition", "pagination_documented": True,
                            "pages_fetched": 0, "requests_used": 1, "records": 0, "next_cursor": "",
                            "truncated": True, "stop_reason": "provider_failed",
                            "detail": "HTTP 429"}],
        },
        "audit": resource_audit.run_coverage(documents, budget),
    }

    return payload


def build() -> dict:
    """The normalized structure one `re0 paper search --json` run would have written."""
    return result_model.normalize(source_payload())


def main(destination: pathlib.Path = OUT) -> pathlib.Path:
    structure = build()
    destination.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(structure, ensure_ascii=False, indent=2)
    destination.write_text(text + chr(10), encoding="utf-8")
    print(f"wrote {destination} ({destination.stat().st_size} bytes, "
          f"{len(structure['documents'])} documents, "
          f"{len(structure['coverage']['source_failures'])} source failures)")
    return destination


if __name__ == "__main__":
    main()
