#!/usr/bin/env python3
"""Multi-source literature search through Re0's retrieval layer.

    python scripts/paper_search.py --query "layout generation" --start-year 2024

Credentials come from the environment. `RE0_ENV_FILE`, then `./.env`, then the
conventional agent-skill locations are tried in order; a variable that is already set
always wins, and values are never printed, logged or written anywhere.

One query per invocation. Merging across sources happens inside Re0, so a second
query is a second invocation rather than a second merge pass.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "backend"))

from re0.agent.tools import ResearchTools          # noqa: E402
from re0.env_file import load                      # noqa: E402
from re0.literature import PUBLICATION_LABELS, artifact_urls  # noqa: E402
from re0.providers import ProviderError, check_resource  # noqa: E402

ENV_FILES = (".env", "~/.codex/skills/.env", "~/.re0/.env")
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
    """Fill unset variables from the first usable file. Reports the file, never a value."""
    explicit = os.getenv("RE0_ENV_FILE", "").strip()
    for candidate in ([explicit] if explicit else list(ENV_FILES)):
        if not candidate:
            continue
        applied = load(candidate)
        if applied:
            return f"{candidate} ({len(applied)} variables set)"
    return "no environment file found; using the ambient environment only"


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


def verify_candidate(url: str) -> None:
    """Run Re0's bounded resource check on one candidate link.

    This is the answer to the two cases a link alone cannot distinguish: a repository
    that exists but is empty, and a link that no longer resolves. The status vocabulary
    is deliberately blunt — an unanswered request is reported as unverified, never as
    "not released" — so the output stays comparable between papers.
    """
    try:
        observation = check_resource(url)
    except Exception:
        # Never relay a raw traceback; an exception is not evidence about the resource.
        print(f"     → {url}\n       verification did not complete (unexpected error); unverified")
        return
    print(f"     → {url}")
    print(f"       status {observation.status} · depth {observation.depth} · provider {observation.provider}")
    print(f"       {observation.summary[:300]}")
    if observation.indicators:
        print("       candidate files: " + ", ".join(f"{kind}={len(hits)}" for kind, hits in observation.indicators.items()))
    for limitation in observation.limitations[:2]:
        print(f"       limit: {limitation}")


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


def print_document(index: int, document: dict, raw: bool, verify_budget: int) -> int:
    """Print one result. Returns how many artifact candidates were actually verified."""
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
    used = 0
    for url in artifact_urls(paper.get("abstract", "")):
        if used >= verify_budget:
            # Still shown, just not fetched: an unverified link is not a failed one.
            print(f"     artifact candidate (from the abstract, unverified): {url}")
            continue
        used += 1
        verify_candidate(url)
    if raw:
        print(document["content"])
    elif paper.get("abstract"):
        print("     abstract: " + paper["abstract"][:EXCERPT_CHARS]
              + ("…" if len(paper["abstract"]) > EXCERPT_CHARS else ""))
    return used


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--query", required=True, help="a focused search phrase")
    parser.add_argument("--venue", default=None, metavar="NAME",
                        help="prepend a venue name (CVPR, NeurIPS, ACL…) to the query. A query hint, "
                             "not an API-side venue filter — see effective_query() for why.")
    parser.add_argument("--start-year", type=int, default=None)
    parser.add_argument("--end-year", type=int, default=None)
    parser.add_argument("--max-papers", type=int, default=8,
                        help="per source; the tool bounds this to 8 to keep evidence small")
    parser.add_argument("--sources", default="all",
                        help="'all' or a comma-separated subset of semanticscholar,openalex,arxiv,openreview,crossref")
    parser.add_argument("--json", dest="json_path", default=None,
                        help="write the complete result set here; stdout stays readable")
    parser.add_argument("--raw", action="store_true", help="print every field instead of a table")
    parser.add_argument("--verify", type=int, default=0, metavar="N",
                        help="run Re0's bounded resource check on the first N artifact candidates "
                             "(0 = none, max 5). One check costs about four GitHub requests and the "
                             "anonymous limit is roughly 60 per hour, so keep N small or set GITHUB_TOKEN.")
    args = parser.parse_args(argv)
    if not 0 <= args.verify <= 5:
        parser.error("--verify takes 0-5; each check costs several requests against a shared rate limit")

    print(f"credentials: {load_credentials()}")
    query = effective_query(args.query, args.venue)
    if args.venue:
        print(f"query hint: venue '{args.venue}' prepended (not an API-side filter)")
    if args.sources == "all":
        source = "all"
    else:
        chosen = [part.strip() for part in args.sources.split(",") if part.strip()]
        if len(chosen) != 1:
            parser.error("--sources takes 'all' or exactly one source name; "
                         "run once per source for a longer list")
        source = chosen[0]
    try:
        result = ResearchTools(None).execute("search_papers", {
            "query": query, "limit": min(8, max(1, args.max_papers)),
            "source": source, "start_year": args.start_year, "end_year": args.end_year})
    except ProviderError as exc:
        # Reported verbatim. A failed search is not an empty result.
        print(f"search failed: {exc}", file=sys.stderr)
        return 2

    print(heading(result, args))
    if result.get("source_failures"):
        print("source failures (these sources were not searched; this is not 'not found'):", file=sys.stderr)
        for failure in result["source_failures"]:
            print(f"  {failure['source']}: {failure['error']}", file=sys.stderr)
    if args.json_path:
        Path(args.json_path).write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"full results written to {args.json_path}")

    documents = ordered(result["documents"])
    if not documents:
        print("no results. An empty result is not evidence that the work does not exist: "
              "widen the query, change the year window, or recheck a source named above.")
        return 0

    budget = args.verify
    for index, document in enumerate(documents, start=1):
        budget -= print_document(index, document, args.raw, budget)
    print("\nThese are bibliographic records, not full text. Confirm anything load-bearing "
          "against the paper itself before citing it.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
