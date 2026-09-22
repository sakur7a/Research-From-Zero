#!/usr/bin/env python3
"""Multi-source literature search through Re0's retrieval layer.

    re0 paper search --query "layout generation" --start-year 2024
    (the skill wrapper `paper_search.py` calls this same entry point)

Credentials come from the environment. An explicit `RE0_ENV_FILE` is honoured exactly, then
`./.env` and `~/.re0/.env`. Another product's credential file is read only when
`RE0_ENV_INCLUDE_AGENT_DIRS=1` asks for it. A variable that is already set always wins, and no
value is ever printed, logged or written anywhere.

One query per invocation. Merging across sources happens inside Re0, so a second
query is a second invocation rather than a second merge pass.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from re0 import resource_audit, resource_matrix, result_model
from re0.agent.tools import ResearchTools
from re0.env_file import load
from re0.literature import PUBLICATION_CAVEAT, PUBLICATION_LABELS
from re0.models import AUDIT_LABELS, COMPONENT_LABELS
from re0.providers import ProviderError

# `./.env` is the caller's own working directory and `~/.re0/.env` is this product's, so both
# stay defaults. Another product's file is NOT searched implicitly: reading whatever account that
# client happened to be signed into is a credential mix-up, not a convenience. Name it with
# RE0_ENV_FILE, or opt in explicitly with RE0_ENV_INCLUDE_AGENT_DIRS=1.
ENV_FILES = (".env", "~/.re0/.env")
AGENT_ENV_FILES = ("~/.codex/skills/.env",)
SURVEY_WORDS = ("survey", "review", "overview", "systematic", "综述")
EXCERPT_CHARS = 1200


def effective_query(query: str, venue: str | None) -> str:
    """Prepend a venue name to the query.

    This is a **query hint, not an API-side venue filter**, because the two filter routes
    are closed: DBLP — the obvious conference index — now answers non-browser clients with an
    anti-bot challenge page instead of JSON, and OpenAlex rejects a source-name filter
    outright (`primary_location.source.display_name.search is not a valid field`). S2 does
    document a `venue` parameter but it could not be verified while rate-limited, so it is
    not used. Putting the venue in the query is verified: "CVPR diffusion model watermarking"
    returns the CVPR paper with its venue named.
    """
    if not venue:
        return query
    return f"{venue} {query}".strip()


def load_credentials() -> str:
    """Fill unset variables from a credential file. Reports the file and how many names were set,
    never a value.

    An explicit `RE0_ENV_FILE` is honoured exactly: when it yields nothing that is reported
    rather than quietly falling back to a different file, because reading credentials the caller
    did not name is how the wrong account gets used.
    """
    explicit = os.getenv("RE0_ENV_FILE", "").strip()
    if explicit:
        applied = load(explicit)
        if applied:
            return f"{explicit} ({len(applied)} variables set)"
        return f"{explicit} was named but supplied nothing (missing, empty, or already set)"
    for candidate in ENV_FILES:
        applied = load(candidate)
        if applied:
            return f"{candidate} ({len(applied)} variables set)"
    if os.getenv("RE0_ENV_INCLUDE_AGENT_DIRS", "").strip().lower() in {"1", "true", "yes"}:
        for candidate in AGENT_ENV_FILES:
            applied = load(candidate)
            if applied:
                return (f"{candidate} ({len(applied)} variables set) — read because "
                        "RE0_ENV_INCLUDE_AGENT_DIRS is set")
    elsewhere = next((path for path in AGENT_ENV_FILES if Path(path).expanduser().is_file()), "")
    hint = (f"; {elsewhere} exists but belongs to another product, so it was not read — pass "
            "RE0_ENV_FILE=<path> or set RE0_ENV_INCLUDE_AGENT_DIRS=1 to use it") if elsewhere else ""
    return "no environment file found; using the ambient environment only" + hint


def is_survey(title: str) -> bool:
    lowered = title.lower()
    return any(word in lowered for word in SURVEY_WORDS)


def ordered(documents: list) -> list:
    """Surveys sink to the bottom but are never dropped: they are often the fastest
    orientation, and hiding them would be a silent editorial decision."""
    return [x for x in documents if not is_survey(x["paper"]["title"])] + \
           [x for x in documents if is_survey(x["paper"]["title"])]


def heading(result: dict, args) -> str:
    counts = result.get("source_counts", {})
    hits = ", ".join(f"{name}={counts.get(name, 0)}" for name in result["sources_queried"])
    line = (f"per-source hits: {hits} · {len(result['documents'])} unique "
            f"({result['duplicates_merged']} duplicates merged)")
    if result.get("dropped_out_of_range"):
        line += f" · {result['dropped_out_of_range']} outside the year window"
    coverage = result.get("coverage") or {}
    if coverage:
        # The same coverage the JSON and MCP exits carry, so the CLI cannot describe one call
        # differently: which attempts ran, and whether the run was complete, partial or a real
        # zero-hit rather than a quiet failure.
        line += (f" · coverage: state={coverage.get('state')} "
                 f"attempts={len(coverage.get('attempts') or [])} "
                 f"succeeded={len(coverage.get('succeeded') or [])} "
                 f"failed={len(coverage.get('failed') or [])}")
        queries = result.get("queries") or []
        if len(queries) > 1:
            line += f" · {len(queries)} queries merged, each record keeps the ones that found it"
    return line


def links_for(paper: dict) -> list:
    """Best link first. An arXiv ID or DOI resolves to the paper itself; a source page is
    only where the record was found, so it comes last and only if nothing better exists."""
    links = []
    if paper.get("arxiv_id"):
        links.append(("arXiv", f"https://arxiv.org/abs/{paper['arxiv_id']}"))
    if paper.get("doi"):
        links.append(("DOI", f"https://doi.org/{paper['doi']}"))
    record = paper.get("paper_url") or ""
    if record and all(record != url for _, url in links):
        links.append(("record", record))
    return links


SHOWN_INSTITUTIONS = 3


def publication_line(document: dict) -> str:
    """Accepted, submitted or preprint — and the raw venue string, so a wrong call is checkable."""
    paper, publication = document["paper"], document.get("publication") or {}
    status = PUBLICATION_LABELS.get(publication.get("state", "unknown"), "无可用信息")
    if publication.get("venue"):
        status += f" · {publication['venue']}"
    if publication.get("source"):
        status += f"（据 {publication['source']}）"
    year = paper.get("year") or "年份未知"
    return f"     {year} · {status}"


def institutions_line(document: dict) -> str:
    institutions = document.get("institutions") or []
    if not institutions:
        # Not "unaffiliated": the services simply did not state it, and coverage is uneven.
        return "     机构: 各来源均未提供"
    shown = ", ".join(institutions[:SHOWN_INSTITUTIONS])
    if len(institutions) > SHOWN_INSTITUTIONS:
        shown += f"（另有 {len(institutions) - SHOWN_INSTITUTIONS} 个）"
    return "     机构: " + shown


SOURCE_CREDENTIALS = {
    "semanticscholar": (("SEMANTIC_SCHOLAR_API_KEY", "SEMANTICSCHOLAR_API_KEY"),
                        "匿名调用会被硬限流，这是 429 的主要原因；而且该源会把同一工作的多个版本合并并给出 venue"),
    "openalex": (("OPENALEX_MAILTO",), "填一个邮箱即可进入礼貌池，降低被限流的概率"),
    "openreview": (("OPENREVIEW_TOKEN",), "只有需要超出公开检索的读取范围时才需要"),
}


def failure_hint(source: str) -> str:
    """A failing source is worth one actionable line, not just an error string."""
    entry = SOURCE_CREDENTIALS.get(source)
    if not entry:
        return ""
    names, reason = entry
    if any(os.getenv(name) for name in names):
        return ""
    return f"  ← 未配置 {' 或 '.join(names)}：{reason}"


def audit_lines(row: dict) -> list:
    """One audit row as text. Every value comes off the row, so the terminal cannot say something
    the JSON does not."""
    if row["status"] == "not_checked":
        # Still listed: an unverified candidate is not a failed one. Keyed off the state, not the
        # depth — a check that ran and failed reached no depth either, and printing that as "未核验"
        # would report an attempt nobody needs to retry as one nobody made.
        reason = "检查未完成" if row.get("provider_status") == "check_error" else "未核验"
        return [f"     开源候选（{row['candidate_origin']}，{reason}）: {row['resource_url']}"]
    provider = row["provider"] or "未知"
    # The provider's own status is shown only when it says something ours does not: a 404 and a
    # 429 both land on `access_failed`, and the difference is the whole point of keeping it.
    extra = row.get("provider_status") or ""
    provider_note = f"；提供商状态 {extra}" if extra and extra != row["status"] else ""
    lines = [f"     → {row['resource_url']}",
             f"       状态 {AUDIT_LABELS[row['status']]}（{row['status']}）{provider_note}"
             f" · 深度 {row['verification_depth']} · 提供商 {provider}"]
    if row.get("summary"):
        lines.append(f"       {row['summary'][:300]}")
    lines.append("       资源类别: " + ", ".join(
        f"{name}={COMPONENT_LABELS[finding['state']]}" for name, finding in row["coverage"].items()))
    if row.get("licences"):
        lines.append("       许可证（来源声明，不是使用权限结论）: "
                     + ", ".join(f"{name}={value}" for name, value in row["licences"].items()))
    if row.get("version_match") != "unknown":
        lines.append(f"       论文/资源版本: {row['version_match']}（{row['version_evidence'][:120]}）")
    lines.extend(f"       limit: {text}" for text in row["limitations"][:3])
    return lines


def no_candidate_line(state: str, detail: dict) -> str:
    """Printed even when nothing was found, so an absence reads as a finding rather than an
    omission — and so "nobody searched" never looks like "searched and found nothing"."""
    name = detail.get("project_name") or ""
    if state == "skipped":
        return ("     开源线索: 摘要中未提及链接，标题里也没有可检索的项目名（形如「Name: ...」）")
    if detail.get("failures"):
        return (f"     开源线索: 摘要中未提及链接；按项目名 '{name}' 的检索未完成（原因见下），"
                "这不代表没有开源，重跑一次通常即可")
    if state == "searched":
        return f"     开源线索: 摘要与元数据均无链接，按项目名 '{name}' 检索 GitHub/HF 也无结果"
    reason = detail.get("reason_not_run") or "disabled"
    return (f"     开源线索: 摘要中未提及链接；按项目名 '{name}' 的 GitHub/HF 检索未开启"
            f"（原因 {reason}；用 --find-artifacts N，默认开 10 篇）")


def print_audit(document: dict) -> None:
    rows = document.get("resource_audits") or []
    detail = document.get("artifact_search_detail") or {}
    for row in rows:
        print("\n".join(audit_lines(row)))
    if not rows:
        print(no_candidate_line(document.get("artifact_search", "not-run"), detail))
    # After the "原因见下" line, so the reasons are where the text says they are.
    for failure in detail.get("failures") or []:
        print(f"     名称检索未完成: {failure}")
    unchecked = sum(1 for row in rows if row["status"] == "not_checked")
    if unchecked:
        print(f"     ↑ 另有 {unchecked} 条未核验；提高 --verify 可继续核验")


def print_audit_coverage(coverage: dict) -> None:
    """The run's own denominators.

    "0 verified" is either "nothing to verify" or "the budget ran out", and only the second is a
    gap, so every count here is printed next to what it is out of.
    """
    search, candidates = coverage["name_search"], coverage["candidates"]
    states = search["states"]
    print(f"\n开源审计覆盖（分母 {search['denominator']} 篇）："
          f"检索完成 {states['searched']} · 部分完成 {states['partial']} · 全部失败 {states['failed']}"
          f" · 无项目名可检索 {states['skipped']} · 未检索 {states['not-run']}"
          f"（名称检索预算 {search['budget']}，已用 {search['spent']}）")
    if states["not-run"] and search["spent"] >= search["budget"] > 0:
        print("  ← 未检索的论文是预算用尽所致，不是没有开源；--find-artifacts 可调大"
              "（GitHub 搜索限 10 次/分钟）。")
    print(f"候选 {candidates['found']} 个（作者自述 {candidates['declared']}，"
          f"名称匹配 {candidates['name_matched']}）；已核验 {candidates['verified']}，"
          f"未核验 {candidates['unchecked']}（核验预算 {candidates['budget']}，"
          f"剩余 {candidates['remaining']}）")
    described = " · ".join(f"{AUDIT_LABELS[state]} {count}"
                           for state, count in coverage["audits"].items() if count)
    if described:
        print("审计结论分布: " + described)
    if coverage["failures"]:
        print(f"名称检索失败 {len(coverage['failures'])} 次（明细见对应论文行；"
              "失败不是「没有结果」）")
    print("候选是名称匹配而非作者身份证明；未核验与核验失败都不表示资源未开放。")


def print_document(index: int, document: dict, raw: bool) -> None:
    """Render one result. Fetches nothing.

    Discovery and verification already ran in `resource_audit`, so what is printed here and what
    `--json` writes come from the same object and cannot describe one run two ways.
    """
    paper = document["paper"]
    tag = "[survey] " if is_survey(paper["title"]) else ""
    print(f"\n{index:>3}. {tag}{paper['title']}")
    print(publication_line(document))
    print(institutions_line(document))
    if document.get("preprint_also"):
        print("     注: 同一工作另有预印本版本被索引")
    for label, url in links_for(paper):
        print(f"     {label}: {url}")
    print(f"     来源: {document['locator']}")
    print_audit(document)
    if raw:
        print(document["content"])
    elif paper.get("abstract"):
        print("     abstract: " + paper["abstract"][:EXCERPT_CHARS]
              + ("…" if len(paper["abstract"]) > EXCERPT_CHARS else ""))


def build_parser() -> argparse.ArgumentParser:
    """The parser is separate from `main()` so a test can hold the parameter contract."""
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--query", default=None,
                        help="a focused phrase. Several short queries recall more than one long "
                             "one: arXiv and Semantic Scholar treat space-separated terms "
                             "restrictively, so 'layer decomposition' beats 'layered representation "
                             "decomposition single image into layers'.")
    parser.add_argument("--queries", default=None, metavar="A|B|C",
                        help="up to 5 short queries in one budgeted call, separated by '|'. One "
                             "merged list comes back, each record keeps which queries found it, and "
                             "the coverage block lists every (query, source) attempt. Use this "
                             "rather than running the command repeatedly and merging by hand.")
    parser.add_argument("--venue", default=None, metavar="NAME",
                        help="prepend a venue name (CVPR, NeurIPS, ACL…) to each query. A query hint, "
                             "not an API-side venue filter — see effective_query() for why.")
    parser.add_argument("--start-year", type=int, default=None)
    parser.add_argument("--end-year", type=int, default=None)
    parser.add_argument("--max-papers", type=int, default=20,
                        help="per source (default 20, tool maximum 25). This is the recall ceiling: "
                             "raise it before adding more queries.")
    parser.add_argument("--sources", default="all",
                        help="'all', one source, or a comma-separated subset "
                             "(e.g. openalex,arxiv) for rechecking a group together. The contract "
                             "accepts up to 5.")
    parser.add_argument("--json", dest="json_path", default=None,
                        help="write the versioned result structure here (the same shape the MCP "
                             "surface returns, with schema_version); stdout stays readable")
    parser.add_argument("--raw", action="store_true", help="print every field instead of a table")
    parser.add_argument("--find-artifacts", type=int, default=10, metavar="N",
                        help="search GitHub and the Hugging Face Hub by each paper's project name for "
                             "the first N papers (default 10, max 10, 0 disables). Most papers carry no "
                             "link in any metadata field yet do have a released repository, so this is "
                             "the only way to surface those. Every hit is a NAME MATCH, not proof of "
                             "authorship, and is labelled as such.")
    parser.add_argument("--verify", type=int, default=0, metavar="N",
                        help="run Re0's bounded resource check on the first N candidates per run "
                             "(0 = none, default 0, max 8). Off by default because one check costs "
                             "about four GitHub CORE requests, whose anonymous limit is 60/hour; the "
                             "search endpoint used by --find-artifacts is limited to 10/minute. Set "
                             "GITHUB_TOKEN to raise both. Unchecked candidates are still listed.")
    parser.add_argument("--resource-matrix", default=None, metavar="PREFIX",
                        help="write the resource matrix beside this prefix as PREFIX.json, PREFIX.md "
                             "and PREFIX.csv: one row per resource candidate, with its audit state, "
                             "per-class coverage, licence declarations and limits. Useful for "
                             "choosing a baseline across 2-6 papers. The CSV escapes cells a "
                             "spreadsheet would run as a formula, and only http(s) links are "
                             "written. Nothing is verified again to build it.")
    return parser


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not 0 <= args.verify <= 8:
        parser.error("--verify takes 0-8; each check costs several requests against a shared rate limit")
    if not 0 <= args.find_artifacts <= 10:
        parser.error("--find-artifacts takes 0-10; the GitHub search endpoint allows 10 requests a minute")

    print(f"credentials: {load_credentials()}")
    # Several queries in one call is the point of `--queries`: the merge happens once, every
    # (query, source) attempt is reported, and no JSON files have to be combined by hand.
    if args.queries:
        queries = [part.strip() for part in args.queries.split("|") if part.strip()]
    elif args.query:
        queries = [args.query]
    else:
        parser.error("pass --query or --queries")
    if len(queries) > 5:
        parser.error("--queries takes at most 5 phrases; more than that is better split into "
                     "separate runs so each stays inside its own budget")
    queries = [effective_query(item, args.venue) for item in queries]
    if args.venue:
        print(f"query hint: venue '{args.venue}' prepended to {len(queries)} query(ies) "
              "(not an API-side filter)")
    if args.sources == "all":
        chosen_sources = None
    else:
        chosen_sources = [part.strip() for part in args.sources.split(",") if part.strip()]
        if not chosen_sources or len(chosen_sources) > 5:
            parser.error("--sources takes 'all', one source, or a comma-separated subset of at most 5")
    payload = {"limit": min(25, max(1, args.max_papers)),
               "start_year": args.start_year, "end_year": args.end_year}
    if len(queries) == 1:
        payload["query"] = queries[0]
    else:
        payload["queries"] = queries
    if chosen_sources:
        payload["sources"] = chosen_sources
    else:
        payload["source"] = "all"
    tools = ResearchTools(None)
    try:
        result = tools.execute("search_papers", payload)
    except ProviderError as exc:
        # Reported verbatim. A failed search is not an empty result.
        print(f"search failed: {exc}", file=sys.stderr)
        return 2

    print(heading(result, args))
    if result.get("source_failures"):
        print("source failures (these sources were not searched; this is not 'not found'):", file=sys.stderr)
        for failure in result["source_failures"]:
            print(f"  {failure['source']}: {failure['error']}" + failure_hint(failure["source"]),
                  file=sys.stderr)
    documents = ordered(result["documents"])
    if not documents:
        print("no results. An empty result is not evidence that the work does not exist: "
              "widen the query, change the year window, or recheck a source named above.")
        return 0

    budget = resource_audit.Budget(find=args.find_artifacts, verify=args.verify)
    for index, document in enumerate(documents, start=1):
        # Audited before it is printed, so the terminal and --json read the same object.
        resource_audit.audit_document(document, tools, budget)
        print_document(index, document, args.raw)
    # The run's own coverage, with denominators: a count without one reads as a result.
    result["audit"] = resource_audit.run_coverage(documents, budget)
    print_audit_coverage(result["audit"])
    if args.json_path:
        # The same versioned structure the MCP surface returns, so the two machine-readable exits
        # cannot describe the same call differently.
        structure = result_model.normalize(result)
        Path(args.json_path).write_text(json.dumps(structure, ensure_ascii=False, indent=2),
                                        encoding="utf-8")
        print(f"完整结果已写入 {args.json_path}"
              f"（schema_version {structure['schema_version']}；文档正文在 documents[].body.excerpt，"
              "每篇的 resource_audits、artifact_candidates 与 artifact_search 都在，"
              "本次审计覆盖在 audit）")
    if args.resource_matrix:
        written = resource_matrix.write(args.resource_matrix, documents, result["audit"])
        print("资源矩阵已写入 " + "、".join(str(path) for path in written)
              + "（每行一个资源候选；CSV 已对公式前缀转义）")
    if any((document.get("publication") or {}).get("state") == "preprint" for document in documents):
        print("\n关于发表状态：" + PUBLICATION_CAVEAT)
    print("\nThese are bibliographic records, not full text. Confirm anything load-bearing "
          "against the paper itself before citing it.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
