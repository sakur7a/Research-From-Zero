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
from re0.providers import ProviderError            # noqa: E402

ENV_FILES = (".env", "~/.codex/skills/.env", "~/.re0/.env")
SURVEY_WORDS = ("survey", "review", "overview", "systematic", "综述")
EXCERPT_CHARS = 1200


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


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--query", required=True, help="a focused search phrase")
    parser.add_argument("--start-year", type=int, default=None)
    parser.add_argument("--end-year", type=int, default=None)
    parser.add_argument("--max-papers", type=int, default=8,
                        help="per source; the tool bounds this to 8 to keep evidence small")
    parser.add_argument("--sources", default="all",
                        help="'all' or a comma-separated subset of semanticscholar,openalex,arxiv,openreview,crossref")
    parser.add_argument("--json", dest="json_path", default=None,
                        help="write the complete result set here; stdout stays readable")
    parser.add_argument("--raw", action="store_true", help="print every field instead of a table")
    args = parser.parse_args(argv)

    print(f"credentials: {load_credentials()}")
    sources = args.sources if args.sources == "all" else args.sources.split(",")
    try:
        result = ResearchTools(None).execute("search_papers", {
            "query": args.query, "limit": min(8, max(1, args.max_papers)),
            "source": sources, "start_year": args.start_year, "end_year": args.end_year})
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

    for index, document in enumerate(documents, start=1):
        paper = document["paper"]
        tag = "[survey] " if is_survey(paper["title"]) else ""
        print(f"\n{index:>3}. {tag}{paper['title']}")
        print(f"     {paper.get('year') or 'year unknown'} · {paper.get('venue') or 'no venue'} · {document['locator']}")
        identifiers = [f"{key}: {paper[key]}" for key in ("doi", "arxiv_id") if paper.get(key)]
        if identifiers:
            print("     " + " · ".join(identifiers))
        if paper.get("paper_url"):
            print(f"     {paper['paper_url']}")
        if args.raw:
            print(document["content"])
        elif paper.get("abstract"):
            print("     abstract: " + paper["abstract"][:EXCERPT_CHARS]
                  + ("…" if len(paper["abstract"]) > EXCERPT_CHARS else ""))
    print("\nThese are bibliographic records, not full text. Confirm anything load-bearing "
          "against the paper itself before citing it.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
