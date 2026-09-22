"""Multi-source scholarly retrieval with cross-source de-duplication.

Source order is signal order. The first source to report a work supplies its
bibliographic fields; a later duplicate only contributes its name, a citation count
and any identifier the first source was missing. Merging therefore accumulates
provenance instead of overwriting it.

A source that fails is reported and skipped. A rate limit, an expired token or an
outage is not evidence that a paper does not exist, and one broken source must never
fail the others.

Credentials are read from the environment at call time and are only ever sent to the
provider that owns them: `OPENALEX_API_KEY` / `OPENALEX_MAILTO`,
`SEMANTIC_SCHOLAR_API_KEY` (alias `SEMANTICSCHOLAR_API_KEY`), `OPENREVIEW_TOKEN`.
Nothing in this module reads a file; see `re0.env_file` for the opt-in loader.
"""
from __future__ import annotations

import datetime
import os
import re
from urllib.parse import urlsplit

from .models import PaperInput, normalize_arxiv
from .providers import ProviderClient

# Signal order, strongest first, as reported by the reference implementation this
# follows. `arxiv` and `crossref` live in `agent/tools.py`; the rest are here.
SOURCE_ORDER = ("semanticscholar", "openalex", "arxiv", "openreview", "crossref")
REMOTE_SOURCES = ("semanticscholar", "openalex", "openreview")
MAX_AUTHORS = 30
MAX_ABSTRACT = 8000
MIN_YEAR, MAX_YEAR = 1800, 2100


def _env(*names: str) -> str:
    for name in names:
        value = os.getenv(name, "").strip()
        if value:
            return value
    return ""


def _text(value, limit: int) -> str:
    return " ".join(str(value or "").split())[:limit]


def _year(value) -> int | None:
    try:
        year = int(str(value)[:4])
    except (TypeError, ValueError):
        return None
    return year if MIN_YEAR <= year <= MAX_YEAR else None


def _epoch_year(value) -> int | None:
    if not isinstance(value, (int, float)) or value <= 0:
        return None
    try:
        return datetime.datetime.fromtimestamp(value / 1000, tz=datetime.timezone.utc).year
    except (OverflowError, OSError, ValueError):
        return None


def _citations(value) -> int | None:
    return value if isinstance(value, int) and 0 <= value <= 10**9 else None


def _authors(names) -> list[str]:
    cleaned = []
    for name in names or []:
        text = _text(name, 160)
        if text and text not in cleaned:
            cleaned.append(text)
    return cleaned[:MAX_AUTHORS]


def _ordered_unique(values) -> list:
    """Trim, drop empties and de-duplicate while keeping the order the source gave, so
    the first entries stay the ones the lead authors are attached to."""
    out: list[str] = []
    for value in values:
        text = _text(value, 200)
        if text and text not in out:
            out.append(text)
    return out


def paper_record(source: str, paper: PaperInput, *, citations=None, venue: str = "",
            publication_state: str = "unknown", publication_venue: str = "",
            institutions=()) -> dict:
    return {"source": source, "paper": paper, "citations": _citations(citations),
            "venue": _text(venue, 200) or paper.venue,
            "publication": {"state": publication_state, "venue": _text(publication_venue, 200),
                            "source": source},
            "institutions": _ordered_unique(institutions)}


# A venue string can describe a preprint server, a review stage or a real venue. These
# lists are deliberately short and visible: the raw string is always printed alongside
# the classification, so a wrong guess is checkable rather than hidden.
PREPRINT_HINTS = ("arxiv", "biorxiv", "medrxiv", "preprint", "ssrn", "research square", "techrxiv")
REVIEW_HINTS = ("submission", "under review", "workshop proposal", "withdrawn", "rejected")

PUBLICATION_RANK = {"venue": 3, "under_review": 2, "preprint": 1, "unknown": 0}
# These describe what the services reported, not what is true of the paper. "仅见预印本版本"
# rather than "仅预印本" is deliberate: a preprint and its published version are normally two
# separate records, so a conference record this run did not reach must not be reported as the
# absence of one.
PUBLICATION_LABELS = {"venue": "有会议或期刊版本", "preprint": "仅见预印本版本",
                      "under_review": "投稿或评审中", "unknown": "来源未给出发表信息"}
PUBLICATION_CAVEAT = ("「仅见预印本版本」指本次检索到的来源里没有会议/期刊版本，不等于不存在："
                      "预印本与已发表版本通常是两条独立记录。配置 SEMANTIC_SCHOLAR_API_KEY 会显著改善，"
                      "因为该源会把同一工作的多个版本合并并给出 venue。")


def classify_venue(name: str) -> str:
    """`venue` when a journal or conference is named, `preprint` when only a preprint
    server is, `under_review` when the record describes a submission, else `unknown`.

    Absence is `unknown`, never `preprint`: a source that carries no venue has told us
    nothing, and calling that "just a preprint" would be an invented conclusion.
    """
    lowered = (name or "").strip().lower()
    if not lowered:
        return "unknown"
    if any(hint in lowered for hint in REVIEW_HINTS):
        return "under_review"
    if any(hint in lowered for hint in PREPRINT_HINTS):
        return "preprint"
    return "venue"


def finalize_publication(claims: list) -> tuple[dict, bool]:
    """Pick the strongest claim and report whether a preprint also exists.

    A paper is often both: an arXiv version plus a published one. Reporting only the
    winner would hide the preprint, and reporting only the preprint would hide the
    acceptance, so the weaker claim is summarised rather than dropped.

    Returns `(publication, preprint_also)`.
    """
    if not claims:
        return {"state": "unknown", "venue": "", "source": "", "sources": []}, False
    best = max(claims, key=lambda claim: PUBLICATION_RANK.get(claim.get("state"), 0))
    sources = sorted({claim.get("source", "") for claim in claims} - {""})
    preprint_also = best.get("state") != "preprint" and any(
        claim.get("state") == "preprint" for claim in claims)
    return ({"state": best.get("state", "unknown"), "venue": best.get("venue", ""),
             "source": best.get("source", ""), "sources": sources}, preprint_also)


def collect_institutions(records: list) -> list:
    """Institution names in first-seen order across records.

    Coverage is genuinely uneven — Semantic Scholar fills affiliations for preprints where
    OpenAlex often has none, and neither fills them for everything — so an empty list means
    "the services did not say", not "the authors are unaffiliated".
    """
    out: list[str] = []
    for record in records:
        for name in record.get("institutions") or []:
            if name not in out:
                out.append(name)
    return out



def _openalex_abstract(index) -> str:
    """OpenAlex ships abstracts as an inverted index; rebuild the plain text."""
    if not isinstance(index, dict):
        return ""
    positions = []
    for word, spots in index.items():
        if not isinstance(spots, list):
            continue
        for spot in spots:
            if isinstance(spot, int) and 0 <= spot <= MAX_ABSTRACT:
                positions.append((spot, word))
    positions.sort()
    return _text(" ".join(word for _, word in positions), MAX_ABSTRACT)


def search_openalex(client: ProviderClient, query: str, limit: int, *, start_year=None, end_year=None) -> list[dict]:
    params: dict = {"search": query, "per-page": limit}
    filters = []
    if start_year or end_year:
        filters.append("from_publication_date:%d-01-01" % (start_year or MIN_YEAR))
        filters.append("to_publication_date:%d-12-31" % (end_year or MAX_YEAR))
    if filters:
        params["filter"] = ",".join(filters)
    mailto = _env("OPENALEX_MAILTO")
    if mailto:
        params["mailto"] = mailto  # identifies the caller for the polite pool, not a secret
    api_key = _env("OPENALEX_API_KEY")
    if api_key:
        params["api_key"] = api_key
    data = client.json("https://api.openalex.org/works", params)
    records = []
    for item in (data.get("results") or [])[:limit]:
        title = _text(item.get("title"), 600)
        if not title:
            continue
        location = item.get("primary_location") or {}
        source = location.get("source") or {}
        source_name = _text(source.get("display_name"), 200)
        source_type = _text(source.get("type"), 40).lower()
        work_type = _text(item.get("type"), 40).lower()
        state = classify_venue(source_name)
        # OpenAlex says outright when a work is a preprint or sits in a repository, which
        # is stronger evidence than the venue string alone.
        if source_type == "repository" or work_type == "preprint":
            state = "preprint"
        doi = _text(item.get("doi"), 300).removeprefix("https://doi.org/")
        paper = PaperInput(
            title=title,
            doi=doi,
            authors=_authors((entry.get("author") or {}).get("display_name") for entry in item.get("authorships") or []),
            year=_year(item.get("publication_year")),
            venue=source_name,
            abstract=_openalex_abstract(item.get("abstract_inverted_index")),
            arxiv_id=arxiv_id_from_doi(doi),
            paper_url=_text(item.get("doi") or item.get("id") or "https://openalex.org", 2000))
        records.append(paper_record("openalex", paper, citations=item.get("cited_by_count"),
                               venue=source_name, publication_state=state, publication_venue=source_name,
                               institutions=(institution.get("display_name")
                                             for entry in item.get("authorships") or []
                                             for institution in entry.get("institutions") or [])))
    return records


def search_semanticscholar(client: ProviderClient, query: str, limit: int, *, start_year=None, end_year=None) -> list[dict]:
    params: dict = {"query": query, "limit": limit,
                    "fields": "title,abstract,year,authors.name,authors.affiliations,externalIds,"
                              "venue,publicationVenue,citationCount,url"}
    if start_year and end_year:
        params["year"] = f"{start_year}-{end_year}"
    elif start_year:
        params["year"] = f"{start_year}-"
    elif end_year:
        params["year"] = f"-{end_year}"
    # The canonical name is SEMANTIC_SCHOLAR_API_KEY; the alias matches existing tools.
    key = _env("SEMANTIC_SCHOLAR_API_KEY", "SEMANTICSCHOLAR_API_KEY")
    data = client.json("https://api.semanticscholar.org/graph/v1/paper/search", params,
                       {"x-api-key": key} if key else None)
    records = []
    for item in (data.get("data") or [])[:limit]:
        title = _text(item.get("title"), 600)
        if not title:
            continue
        external = item.get("externalIds") or {}
        doi = _text(external.get("DOI"), 300)
        # `publicationVenue` is the structured venue; `venue` is the free-text one. A
        # venue of "arXiv.org" means the record is a preprint, not a publication.
        venue = _text((item.get("publicationVenue") or {}).get("name"), 200) or _text(item.get("venue"), 200)
        paper = PaperInput(
            title=title,
            authors=_authors((entry.get("name") for entry in item.get("authors") or [])),
            year=_year(item.get("year")),
            venue=venue,
            abstract=_text(item.get("abstract"), MAX_ABSTRACT),
            doi=doi,
            arxiv_id=_text(external.get("ArXiv"), 100) or arxiv_id_from_doi(doi),
            paper_url=_text(item.get("url"), 2000))
        records.append(paper_record("semanticscholar", paper, citations=item.get("citationCount"),
                               venue=venue, publication_state=classify_venue(venue), publication_venue=venue,
                               institutions=((entry.get("affiliations") or [None])[0]
                                             for entry in item.get("authors") or [])))
    return records


def search_openreview(client: ProviderClient, query: str, limit: int, *, start_year=None, end_year=None) -> list[dict]:
    """Public note search, which needs no account. A bearer token widens what is readable.

    Credentialed access uses `OPENREVIEW_TOKEN` rather than a username/password pair:
    posting a password from inside a retrieval path would add a credential-handling
    surface for no gain while public search already answers a terms query.
    """
    token = _env("OPENREVIEW_TOKEN")
    data = client.json("https://api2.openreview.net/notes/search",
                       {"term": query, "limit": limit, "source": "all"},
                       {"Authorization": "Bearer " + token} if token else None)
    records = []
    for note in (data.get("notes") or [])[:limit]:
        content = note.get("content") or {}

        def value(field, default=""):
            raw = content.get(field)
            return raw.get("value", default) if isinstance(raw, dict) else (raw or default)

        title = _text(value("title"), 600)
        if not title:
            continue
        note_id = _text(note.get("forum") or note.get("id"), 100)
        # OpenReview is where submissions and their decisions live, so its venue string is
        # the strongest acceptance signal available here: a decision reads "ICLR 2025 Oral",
        # an undecided paper reads "ICLR 2025 Conference Submission".
        venue = _text(value("venue"), 200)
        paper = PaperInput(
            title=title,
            authors=_authors(value("authors", [])),
            year=_year(_epoch_year(note.get("cdate"))) or _year(_epoch_year(note.get("tcdate"))),
            venue=venue,
            abstract=_text(value("abstract"), MAX_ABSTRACT),
            paper_url=f"https://openreview.net/forum?id={note_id}" if note_id else "https://openreview.net")
        records.append(paper_record("openreview", paper, venue=venue,
                               publication_state=classify_venue(venue), publication_venue=venue))
    return records


CONNECTORS = {"openalex": search_openalex, "semanticscholar": search_semanticscholar,
              "openreview": search_openreview}


# A normalised title is the last-resort identifier, and a short one is too easy to
# collide on ("Survey", "Introduction"), so it only counts at or above this length.
TITLE_KEY_MIN_CHARS = 16


def paper_keys(paper: PaperInput) -> list[str]:
    """Every identifier this record can be matched on, strongest first.

    Using a single key would fail the common case where one service reports a DOI and
    another only a title, so a record is indexed under all of its identifiers.
    """
    keys = []
    if paper.doi:
        keys.append("doi:" + paper.doi.lower())
    if paper.arxiv_id:
        keys.append("arxiv:" + paper.arxiv_id.lower())
    title = "".join(character for character in paper.title.lower() if character.isalnum())
    if len(title) >= TITLE_KEY_MIN_CHARS:
        keys.append("title:" + title)
    return keys


def is_arxiv_doi(doi: str) -> bool:
    return (doi or "").lower().startswith("10.48550/arxiv")


def identifiers_conflict(first: PaperInput, second: PaperInput) -> bool:
    """True when two records that share a title are nonetheless different works.

    A preprint and its published version legitimately carry different DOIs, and one of them is
    usually the arXiv DOI, so that pair is not a conflict. Two unrelated works with a similar title,
    different non-arXiv DOIs and different years are — merging them would mint a paper that does not
    exist, which is worse than listing both.
    """
    if not (first.doi and second.doi) or first.doi.lower() == second.doi.lower():
        return False
    if is_arxiv_doi(first.doi) or is_arxiv_doi(second.doi):
        return False
    return first.year is not None and second.year is not None and first.year != second.year


def merge_records(records: list[dict]) -> tuple[list[dict], int]:
    """Collapse duplicates across sources. Returns the merged list and the fold count.

    Sources are visited in signal order, so the first service to report a work
    supplies its fields and a later duplicate only adds provenance, a citation count,
    identifiers the first source was missing, and its own publication claim.
    """
    index: dict[str, dict] = {}
    merged: list[dict] = []
    duplicates = 0
    for record in records:
        keys = paper_keys(record["paper"])
        # A record may carry no publication claim at all; `unknown` is the honest default.
        claim = record.get("publication") or {"state": "unknown", "venue": "",
                                              "source": record.get("source", "")}
        match = next(((key, index[key]) for key in keys if key in index), None)
        current = match[1] if match else None
        if current is not None and match[0].startswith("title:") \
                and identifiers_conflict(current["paper"], record["paper"]):
            # A shared title is the weakest key we match on. When the identifiers disagree about
            # which work this is, listing both is correct and merging them would invent one.
            current = None
        if current is None:
            current = {**record, "sources": [record["source"]], "publication_claims": [claim],
                       "queries": list(record.get("queries") or [])}
            merged.append(current)
            keys = keys or [f"unmatched:{len(merged)}"]
        else:
            duplicates += 1
            for query in record.get("queries") or []:
                if query not in current["queries"]:
                    current["queries"].append(query)
            if record["source"] not in current["sources"]:
                current["sources"].append(record["source"])
            if current["citations"] is None or (record["citations"] or 0) > current["citations"]:
                current["citations"] = record["citations"]
            current["publication_claims"].append(claim)
            for name in record.get("institutions") or []:
                if name not in current["institutions"]:
                    current["institutions"].append(name)
            for field in ("doi", "arxiv_id", "venue", "year", "paper_url"):
                if not getattr(current["paper"], field) and getattr(record["paper"], field):
                    setattr(current["paper"], field, getattr(record["paper"], field))
            # A second, different DOI is kept rather than dropped: a preprint and its published
            # version carry separate ones, and losing either makes the record unverifiable.
            incoming = getattr(record["paper"], "doi", "")
            if incoming and incoming.lower() != (current["paper"].doi or "").lower():
                others = current.setdefault("other_dois", [])
                if incoming not in others:
                    others.append(incoming)
            # Keep the longest abstract rather than the first: a longer one is likelier to
            # carry the code or data link the artifact scan looks for.
            if len(record["paper"].abstract) > len(current["paper"].abstract):
                current["paper"].abstract = record["paper"].abstract
        for key in keys:
            index.setdefault(key, current)
    for entry in merged:
        entry["publication"], entry["preprint_also"] = finalize_publication(entry.pop("publication_claims"))
    return merged, duplicates



def in_year_range(year: int | None, start_year=None, end_year=None) -> bool:
    """Filter on a known year only. An unknown year is kept, because dropping it would
    silently hide a real result and "we could not tell" is not "out of range"."""
    if year is None or (not start_year and not end_year):
        return True
    if start_year and year < start_year:
        return False
    if end_year and year > end_year:
        return False
    return True


URL_PATTERN = re.compile(r"https?://[^\s<>()\[\]\"'，。；）】]+", re.IGNORECASE)
# Hosts that hold code, weights or data. A paper's own venue (arXiv, OpenReview) is
# deliberately absent: that link is the record, not an artifact.
ARTIFACT_HOSTS = ("github.com", "huggingface.co", "gitlab.com", "zenodo.org",
                  "figshare.com", "codeocean.com", "bitbucket.org")
# arXiv assigns DOIs of the form 10.48550/arXiv.<id>, so a service that reports only the
# DOI still tells us the arXiv ID — which is the link most readers actually want.
ARXIV_DOI_PREFIX = "10.48550/arxiv."


def arxiv_id_from_doi(doi: str) -> str:
    """Recover an arXiv ID from an arXiv DOI, or return "" when it is not one."""
    value = (doi or "").strip().lower()
    if not value.startswith(ARXIV_DOI_PREFIX):
        return ""
    try:
        return normalize_arxiv(value[len(ARXIV_DOI_PREFIX):])
    except ValueError:
        return ""


def artifact_urls(*texts, limit: int = 5) -> list[str]:
    """Code/data URLs a paper's own text advertises.

    These are candidates, not a verification. A URL in an abstract is an author's claim
    about what they released, which is exactly the thing that still has to be checked, so
    a caller must not read this list as "the code is open".
    """
    found: list[str] = []
    for text in texts:
        for match in URL_PATTERN.findall(str(text or "")):
            url = match.rstrip(".,;:")
            host = (urlsplit(url).hostname or "").lower()
            if not any(host == name or host.endswith("." + name) for name in ARTIFACT_HOSTS):
                continue
            if url not in found:
                found.append(url)
    return found[:limit]
