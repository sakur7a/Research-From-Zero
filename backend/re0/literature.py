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

from .models import PaperInput
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


def _record(source: str, paper: PaperInput, *, citations=None, venue: str = "") -> dict:
    return {"source": source, "paper": paper, "citations": _citations(citations),
            "venue": _text(venue, 200) or paper.venue}


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
        paper = PaperInput(
            title=title,
            doi=_text(item.get("doi"), 300).removeprefix("https://doi.org/"),
            authors=_authors((entry.get("author") or {}).get("display_name") for entry in item.get("authorships") or []),
            year=_year(item.get("publication_year")),
            venue=_text((location.get("source") or {}).get("display_name"), 200),
            abstract=_openalex_abstract(item.get("abstract_inverted_index")),
            paper_url=_text(item.get("doi") or item.get("id") or "https://openalex.org", 2000))
        records.append(_record("openalex", paper, citations=item.get("cited_by_count"), venue=paper.venue))
    return records


def search_semanticscholar(client: ProviderClient, query: str, limit: int, *, start_year=None, end_year=None) -> list[dict]:
    params: dict = {"query": query, "limit": limit,
                    "fields": "title,abstract,year,authors,externalIds,venue,citationCount,url"}
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
        paper = PaperInput(
            title=title,
            authors=_authors((entry.get("name") for entry in item.get("authors") or [])),
            year=_year(item.get("year")),
            venue=_text(item.get("venue"), 200),
            abstract=_text(item.get("abstract"), MAX_ABSTRACT),
            doi=_text(external.get("DOI"), 300),
            arxiv_id=_text(external.get("ArXiv"), 100),
            paper_url=_text(item.get("url"), 2000))
        records.append(_record("semanticscholar", paper, citations=item.get("citationCount"), venue=paper.venue))
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
        paper = PaperInput(
            title=title,
            authors=_authors(value("authors", [])),
            year=_year(_epoch_year(note.get("cdate"))) or _year(_epoch_year(note.get("tcdate"))),
            venue=_text(value("venue"), 200),
            abstract=_text(value("abstract"), MAX_ABSTRACT),
            paper_url=f"https://openreview.net/forum?id={note_id}" if note_id else "https://openreview.net")
        records.append(_record("openreview", paper, venue=paper.venue))
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


def merge_records(records: list[dict]) -> tuple[list[dict], int]:
    """Collapse duplicates across sources. Returns the merged list and the fold count.

    Sources are visited in signal order, so the first service to report a work
    supplies its fields and a later duplicate only adds provenance, a citation count
    and identifiers the first source was missing.
    """
    index: dict[str, dict] = {}
    merged: list[dict] = []
    duplicates = 0
    for record in records:
        keys = paper_keys(record["paper"])
        current = next((index[key] for key in keys if key in index), None)
        if current is None:
            current = {**record, "sources": [record["source"]]}
            merged.append(current)
            keys = keys or [f"unmatched:{len(merged)}"]
        else:
            duplicates += 1
            if record["source"] not in current["sources"]:
                current["sources"].append(record["source"])
            if current["citations"] is None or (record["citations"] or 0) > current["citations"]:
                current["citations"] = record["citations"]
            for field in ("doi", "arxiv_id", "abstract", "venue", "year", "paper_url"):
                if not getattr(current["paper"], field) and getattr(record["paper"], field):
                    setattr(current["paper"], field, getattr(record["paper"], field))
        for key in keys:
            index.setdefault(key, current)
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
