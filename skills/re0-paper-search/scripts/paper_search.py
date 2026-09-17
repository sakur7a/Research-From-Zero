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
import re
import sys
from pathlib import Path

def locate_backend():
    """Find the `re0` package, whether this skill sits inside the repository or not.

    A skill copied into an agent's skills directory is no longer at a fixed depth under the
    repository, so `parents[3]` cannot be relied on. `RE0_HOME` names a checkout explicitly;
    otherwise a repository the skill still lives in is tried; otherwise the ambient
    environment is used as-is (an installed `re0-research`). Nothing is searched broadly, so
    a wrong `RE0_HOME` fails loudly instead of picking up an unrelated package.
    """
    explicit = os.getenv("RE0_HOME", "").strip()
    if explicit:
        candidate = Path(explicit).expanduser() / "backend"
        if (candidate / "re0" / "agent" / "tools.py").is_file():
            return candidate
        print(f"RE0_HOME={explicit} does not look like a Re0 checkout "
              "(no backend/re0/agent/tools.py)", file=sys.stderr)
        raise SystemExit(2)
    here = Path(__file__).resolve()
    # <repo>/skills/<skill>/scripts/paper_search.py -> <repo>
    if len(here.parents) > 3:
        candidate = here.parents[3] / "backend"
        if (candidate / "re0" / "agent" / "tools.py").is_file():
            return candidate
    return None


_backend = locate_backend()
if _backend is not None:
    sys.path.insert(0, str(_backend))

try:
    from re0.agent.tools import ResearchTools          # noqa: E402
    from re0.env_file import load                      # noqa: E402
    from re0.literature import PUBLICATION_CAVEAT, PUBLICATION_LABELS, artifact_urls  # noqa: E402
    from re0.providers import ProviderError, check_resource  # noqa: E402
except ModuleNotFoundError:
    print("re0 is not importable from here. Either install it (pip install -e <repo>) or point\n"
          "this skill at a checkout: RE0_HOME=/path/to/re0 python paper_search.py ...",
          file=sys.stderr)
    raise SystemExit(2)

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


PROJECT_NAME_PATTERN = re.compile(r"[A-Za-z][A-Za-z0-9]*(?:[-_.][A-Za-z0-9]+)*")


def short_name(title: str) -> str:
    """The project name a paper titles itself with: the part before a colon or dash.

    "RevealLayer: Disentangling Hidden and Visible Layers…" -> "RevealLayer", which is also the
    repository name. A sentence-like head is rejected rather than searched, because searching a
    sentence returns noise.
    """
    head = re.split(r"[:\u2014\u2013]|\s-\s", title, maxsplit=1)[0].strip().strip('"\'\u201c\u201d')
    if PROJECT_NAME_PATTERN.fullmatch(head) and 3 <= len(head) <= 40:
        return head
    return ""


def _normalise(value: str) -> str:
    return re.sub(r"[-_.]", "", (value or "").lower())


def candidate_origin(label: str, content: str, title: str, name: str) -> tuple:
    """Rank a name match by how much evidence it carries, because a fuzzy search returns noise.

    A description that repeats the paper title is the strongest cheap signal of ownership, an
    identifier whose last segment equals the project name comes next, and a shared word alone is
    the weakest — `Stability-AI/Stable-Layers` and `nathannlu/aperture` can both come back from
    one query, and the reader needs to see which is which. None of it proves authorship.
    """
    body = content or ""
    try:
        payload = json.loads(body)
    except ValueError:
        payload = {}
    head = " ".join(title.split()[:4]).lower()
    if head and head in body.lower():
        return label + "·描述与论文标题相符", 3
    identifier = str(payload.get("full_name") or payload.get("id") or "")
    if _normalise(identifier.split("/")[-1]) == _normalise(name):
        return label + "·标识名与项目名一致", 2
    return label + "·仅名称匹配", 1


def artifact_candidates(tools, name: str, title: str, per_kind: int, failures: list) -> list:
    """Search GitHub and the Hugging Face Hub by the paper's project name.

    Name matching never establishes authorship, which is why everything here is reported as
    unverified. But a paper whose metadata carries no link very often does have a released
    artifact, and searching is the only way to surface it: the alternative is reporting nothing.
    """
    found = []
    for tool, extra, label in (("search_repositories", {}, "GitHub 名称检索"),
                               ("search_hub", {"kind": "models"}, "HF models 名称检索"),
                               ("search_hub", {"kind": "datasets"}, "HF datasets 名称检索")):
        result, last = None, None
        for attempt in range(2):
            try:
                result = tools.execute(tool, {"query": name, "limit": per_kind, **extra})
                break
            except (ProviderError, ValueError) as exc:
                last = exc
        if result is None:
            failures.append(f"{label}: {last}")
            continue
        for document in result.get("documents", []):
            origin, rank = candidate_origin(label, document.get("content", ""), title, name)
            found.append({"url": document["source_url"], "origin": origin, "rank": rank})
    # Strongest evidence first, so a fuzzy hit cannot sit above the likely official repository.
    return sorted(found, key=lambda entry: -entry["rank"])


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


def print_document(index: int, document: dict, raw: bool, verify_budget: int,
                   tools=None, find_budget: int = 0) -> tuple:
    """Print one result. Returns `(verified, searched)` for the two budgets.

    Candidates come from two places and are verified from one budget: links the authors put in
    the abstract, which are declarations, and links found by searching GitHub and the Hugging Face
    Hub for the paper's project name, which are name matches. The first are listed first because
    they are the stronger claim, but neither is a verification.
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

    candidates = [(url, "摘要中自述") for url in artifact_urls(paper.get("abstract", ""))]
    name = short_name(paper["title"])
    searched = 0
    failures: list = []
    if tools is not None and find_budget > 0 and name:
        searched = 1
        candidates += [(c["url"], c["origin"])
                       for c in artifact_candidates(tools, name, paper["title"], 3, failures)]
    used = 0
    for url, origin in candidates:
        if used >= verify_budget:
            # Still shown, just not fetched: an unverified link is not a failed one.
            print(f"     开源候选（{origin}，未核验）: {url}")
            continue
        used += 1
        verify_candidate(url)
    for failure in failures:
        print(f"     名称检索未完成: {failure}")
    if not candidates:
        # Printed even when empty, so its absence reads as a finding rather than an omission.
        if not name:
            print("     开源线索: 摘要中未提及链接，标题里也没有可检索的项目名（形如「Name: ...」）")
        elif failures:
            print(f"     开源线索: 摘要中未提及链接；按项目名 '{name}' 的检索未完成（原因见下），"
                  "这不代表没有开源，重跑一次通常即可")
        elif searched:
            print(f"     开源线索: 摘要与元数据均无链接，按项目名 '{name}' 检索 GitHub/HF 也无结果")
        else:
            print(f"     开源线索: 摘要中未提及链接；按项目名 '{name}' 的 GitHub/HF 检索未开启"
                  "（用 --find-artifacts N，默认开 5 篇）")
    elif used < len(candidates):
        print(f"     ↑ 另有 {len(candidates) - used} 条未核验；提高 --verify 可继续核验")
    if raw:
        print(document["content"])
    elif paper.get("abstract"):
        print("     abstract: " + paper["abstract"][:EXCERPT_CHARS]
              + ("…" if len(paper["abstract"]) > EXCERPT_CHARS else ""))
    return used, searched


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--query", required=True,
                        help="a focused phrase. Several short queries recall more than one long "
                             "one: arXiv and Semantic Scholar treat space-separated terms "
                             "restrictively, so 'layer decomposition' beats 'layered representation "
                             "decomposition single image into layers'. Run it once per phrase.")
    parser.add_argument("--venue", default=None, metavar="NAME",
                        help="prepend a venue name (CVPR, NeurIPS, ACL…) to the query. A query hint, "
                             "not an API-side venue filter — see effective_query() for why.")
    parser.add_argument("--start-year", type=int, default=None)
    parser.add_argument("--end-year", type=int, default=None)
    parser.add_argument("--max-papers", type=int, default=20,
                        help="per source (default 20, tool maximum 25). This is the recall ceiling: "
                             "raise it before adding more queries.")
    parser.add_argument("--sources", default="all",
                        help="'all' or a comma-separated subset of semanticscholar,openalex,arxiv,openreview,crossref")
    parser.add_argument("--json", dest="json_path", default=None,
                        help="write the complete result set here; stdout stays readable")
    parser.add_argument("--raw", action="store_true", help="print every field instead of a table")
    parser.add_argument("--find-artifacts", type=int, default=5, metavar="N",
                        help="search GitHub and the Hugging Face Hub by each paper's project name for "
                             "the first N papers (default 5, max 10, 0 disables). Most papers carry no "
                             "link in any metadata field yet do have a released repository, so this is "
                             "the only way to surface those. Every hit is a NAME MATCH, not proof of "
                             "authorship, and is labelled as such.")
    parser.add_argument("--verify", type=int, default=0, metavar="N",
                        help="run Re0's bounded resource check on the first N candidates per run "
                             "(0 = none, default 0, max 8). Off by default because one check costs "
                             "about four GitHub CORE requests, whose anonymous limit is 60/hour; the "
                             "search endpoint used by --find-artifacts is limited to 10/minute. Set "
                             "GITHUB_TOKEN to raise both. Unchecked candidates are still listed.")
    args = parser.parse_args(argv)
    if not 0 <= args.verify <= 8:
        parser.error("--verify takes 0-8; each check costs several requests against a shared rate limit")
    if not 0 <= args.find_artifacts <= 10:
        parser.error("--find-artifacts takes 0-10; the GitHub search endpoint allows 10 requests a minute")

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
    tools = ResearchTools(None)
    try:
        result = tools.execute("search_papers", {
            "query": query, "limit": min(25, max(1, args.max_papers)),
            "source": source, "start_year": args.start_year, "end_year": args.end_year})
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
    if args.json_path:
        Path(args.json_path).write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"full results written to {args.json_path}")

    documents = ordered(result["documents"])
    if not documents:
        print("no results. An empty result is not evidence that the work does not exist: "
              "widen the query, change the year window, or recheck a source named above.")
        return 0

    verify_left, find_left = args.verify, args.find_artifacts
    for index, document in enumerate(documents, start=1):
        used_verify, searched = print_document(index, document, args.raw, verify_left, tools, find_left)
        verify_left -= used_verify
        find_left -= searched
    if args.find_artifacts and len(documents) > args.find_artifacts:
        print(f"\n名称检索只覆盖了前 {args.find_artifacts} 篇（--find-artifacts 可调大，GitHub 搜索限 10 次/分钟）。")
    if any((document.get("publication") or {}).get("state") == "preprint" for document in documents):
        print("\n关于发表状态：" + PUBLICATION_CAVEAT)
    print("\nThese are bibliographic records, not full text. Confirm anything load-bearing "
          "against the paper itself before citing it.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
