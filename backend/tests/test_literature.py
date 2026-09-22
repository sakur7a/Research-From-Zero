"""Multi-source retrieval: merge precedence, source isolation and key scoping.

Only the provider HTTP is replaced (`httpx.MockTransport`); merging, parsing and
failure handling are the real code paths.
"""
import importlib.util
import json
import os
from pathlib import Path

import httpx
import pytest
from pydantic import ValidationError

from re0 import resource_audit
from re0.agent.schemas import PaperSearchArgs
from re0.agent.tools import ResearchTools
from re0.env_file import load, parse
from re0.literature import (arxiv_id_from_doi, artifact_urls, classify_venue, finalize_publication,
                            in_year_range, merge_records, paper_keys, paper_record)
from re0.models import Evidence, Observation, PaperInput
from re0.providers import ProviderError

OPENALEX = {
    "results": [
        {"id": "https://openalex.org/W1", "doi": "https://doi.org/10.1000/xyz",
         "title": "Fixture Layout Study", "publication_year": 2024, "cited_by_count": 12,
         "type": "preprint",
         "authorships": [{"author": {"display_name": "Ada Lovelace"},
                          "institutions": [{"display_name": "Microsoft Research"},
                                           {"display_name": "Tsinghua University"}]}],
         "primary_location": {"source": {"display_name": "arXiv (Cornell University)", "type": "repository"}},
         "abstract_inverted_index": {"Layout": [0], "matters": [1], "here": [2]}},
    ]
}
SEMANTIC = {
    "data": [
        {"title": "Fixture Layout Study", "abstract": "Same work, second service.",
         "year": 2024, "citationCount": 30, "venue": "FixtureConf",
         "publicationVenue": {"name": "Neural Information Processing Systems"},
         "externalIds": {"DOI": "10.1000/xyz", "ArXiv": "2401.00001"},
         "authors": [{"name": "Ada Lovelace", "affiliations": ["Microsoft Research"]}],
         "url": "https://www.semanticscholar.org/paper/x"},
        {"title": "A Second Fixture Study Reported Twice", "abstract": "", "year": 2019, "citationCount": 1,
         "externalIds": {"DOI": "10.1000/only"}, "authors": [], "url": "https://example.invalid/s"},
    ]
}
OPENREVIEW = {
    "count": 1,
    "notes": [{"id": "abc", "forum": "abc", "cdate": 1700000000000,
               "content": {"title": {"value": "A Second Fixture Study Reported Twice"},
                           "abstract": {"value": "Third service."},
                           "authors": {"value": ["Grace Hopper"]},
                           "venue": {"value": "ICLR 2024 Conference Submission"}}}],
}
CROSSREF = {"message": {"items": [
    {"DOI": "10.1000/xyz", "title": ["Fixture Layout Study"], "type": "proceedings-article",
     "container-title": ["Neural Information Processing Systems"],
     "issued": {"date-parts": [[2024]]},
     "author": [{"given": "Ada", "family": "Lovelace",
                 "affiliation": [{"name": "Microsoft Research"}]}]}]}}
ARXIV = b'''<feed xmlns="http://www.w3.org/2005/Atom"><entry><id>http://arxiv.org/abs/2401.00001v1</id><title>Fixture Layout Study</title><summary>Preprint of the same work.</summary><published>2024-01-01T00:00:00Z</published><author><name>Ada Lovelace</name></author></entry></feed>'''


def skill_docs() -> str:
    """The skill's documentation set: SKILL.md plus every reference file beside it.

    Splitting the skill into an entry point and references moved prose without moving its readers,
    so a test that reads only SKILL.md would pass while a rule sat unread in a reference — and a
    test that reads only the references would pass while the entry point stopped linking to them.
    This checks both: the text is returned concatenated, and an unlinked reference fails here.
    """
    root = Path(__file__).resolve().parents[2] / "skills" / "re0-paper-search"
    entry = (root / "SKILL.md").read_text(encoding="utf-8")
    parts = [entry]
    references = sorted((root / "references").glob("*.md"))
    assert references, "the skill has no references/ directory"
    for path in references:
        assert f"references/{path.name}" in entry, f"SKILL.md does not link to references/{path.name}"
        parts.append(path.read_text(encoding="utf-8"))
    return "\n".join(parts)


def router(request):
    """A well-behaved world: every source answers with its own shape."""
    host, path = request.url.host, request.url.path
    if host == "api.openalex.org":
        return httpx.Response(200, json=OPENALEX)
    if host == "api.semanticscholar.org":
        return httpx.Response(200, json=SEMANTIC)
    if host == "api2.openreview.net":
        return httpx.Response(200, json=OPENREVIEW)
    if host == "api.crossref.org":
        return httpx.Response(200, json=CROSSREF)
    if host == "export.arxiv.org":
        return httpx.Response(200, content=ARXIV)
    return httpx.Response(404)


def run(transport, **arguments):
    tools = ResearchTools(None, httpx.MockTransport(transport))
    payload = arguments.pop("payload", {"query": "layout", "limit": 5, "source": "all"})
    payload.update(arguments)
    return tools.execute("search_papers", payload)


def test_all_sources_are_queried_and_duplicates_merge_across_them():
    result = run(router)
    assert result["sources_queried"] == ["semanticscholar", "openalex", "arxiv", "openreview", "crossref"]
    titles = [document["paper"]["title"] for document in result["documents"]]
    assert sorted(titles) == ["A Second Fixture Study Reported Twice", "Fixture Layout Study"]
    merged = next(document for document in result["documents"] if document["paper"]["title"] == "Fixture Layout Study")
    # One work reported by four services collapses to one record that keeps all of them.
    assert merged["locator"] == "metadata from semanticscholar, openalex, arxiv, crossref; not full text"
    assert "sources: semanticscholar, openalex, arxiv, crossref" in merged["content"]
    # The highest citation count wins and fields the first source lacked get filled in.
    assert "citations: 30" in merged["content"]
    assert "doi: 10.1000/xyz" in merged["content"]
    assert "arxiv: 2401.00001" in merged["content"]
    # A title-only record merges with a DOI record for the same work (3 + 1 folds).
    second = next(document for document in result["documents"] if document["paper"]["title"].startswith("A Second"))
    assert second["locator"] == "metadata from semanticscholar, openreview; not full text"
    assert second["paper"]["doi"] == "10.1000/only"
    assert result["duplicates_merged"] == 4


def test_a_named_venue_outranks_a_preprint_claim_and_the_preprint_is_still_reported():
    result = run(router)
    published = next(document for document in result["documents"] if document["paper"]["title"] == "Fixture Layout Study")
    # Semantic Scholar and Crossref name a conference; OpenAlex and arXiv call it a preprint.
    assert "publication: 有会议或期刊版本 — Neural Information Processing Systems" in published["content"]
    assert "publication note: a preprint version is also indexed" in published["content"]
    # The weaker claim is summarised, never dropped, and the raw string stays visible.
    assert "arXiv (Cornell University)" not in published["content"].split("publication:")[1].split("\n")[0]
    # Institutions arrive from two services, de-duplicated, in first-seen order.
    assert "institutions: Microsoft Research, Tsinghua University" in published["content"]


def test_a_submission_is_reported_as_under_review_not_as_published():
    result = run(router)
    pending = next(document for document in result["documents"] if document["paper"]["title"].startswith("A Second"))
    # OpenReview says "ICLR 2024 Conference Submission"; calling that accepted would be a lie.
    assert "publication: 投稿或评审中 — ICLR 2024 Conference Submission" in pending["content"]


def test_venue_classification_is_conservative_about_absence():
    assert classify_venue("Neural Information Processing Systems") == "venue"
    assert classify_venue("arXiv (Cornell University)") == "preprint"
    assert classify_venue("bioRxiv") == "preprint"
    assert classify_venue("ICLR 2025 Conference Submission") == "under_review"
    assert classify_venue("") == "unknown"
    assert classify_venue(None) == "unknown"
    # An unstated venue means "nobody said", never "just a preprint".
    assert finalize_publication([]) == ({"state": "unknown", "venue": "", "source": "", "sources": []}, False)
    assert finalize_publication([{"state": "preprint", "venue": "arXiv", "source": "arxiv"}])[1] is False


def test_records_without_a_publication_claim_still_merge():
    # merge_records must not require the sub-structure; an absent claim is `unknown`.
    merged, duplicates = merge_records([
        {"source": "a", "citations": 1, "venue": "", "paper": PaperInput(title="Same Long Enough Title")},
        {"source": "b", "citations": 2, "venue": "", "paper": PaperInput(title="Same Long Enough Title")},
    ])
    assert duplicates == 1 and merged[0]["publication"]["state"] == "unknown"
    assert merged[0]["preprint_also"] is False


def test_the_paper_keys_prefer_doi_then_arxiv_then_a_normalised_title():
    assert paper_keys(PaperInput(title="A very long enough title here", doi="10.1000/X")) == ["doi:10.1000/x", "title:averylongenoughtitlehere"]
    assert paper_keys(PaperInput(title="A very long enough title here", arxiv_id="2401.00001"))[0] == "arxiv:2401.00001"
    # Punctuation and case must not create a second record for the same title.
    long_title = "Attention Is All You Need For Everything"
    assert "title:attentionisallyouneedforeverything" in paper_keys(PaperInput(title=long_title))
    assert "title:attentionisallyouneedforeverything" in paper_keys(PaperInput(title="attention, is all you need for everything!"))
    # A short title is too collision-prone to match on.
    assert paper_keys(PaperInput(title="Survey")) == []


def test_a_record_with_a_doi_still_merges_with_a_title_only_record():
    with_doi = {"source": "semanticscholar", "citations": 30, "venue": "",
                "paper": PaperInput(title="Fixture Layout Study", doi="10.1000/xyz")}
    title_only = {"source": "openreview", "citations": None, "venue": "ICLR 2024",
                  "paper": PaperInput(title="fixture layout study", venue="ICLR 2024")}
    merged, duplicates = merge_records([with_doi, title_only])
    assert duplicates == 1 and len(merged) == 1
    assert merged[0]["sources"] == ["semanticscholar", "openreview"]
    assert merged[0]["paper"].doi == "10.1000/xyz" and merged[0]["paper"].venue == "ICLR 2024"


def test_merge_fills_missing_fields_without_overwriting_the_first_source():
    first = {"source": "openalex", "citations": 5, "venue": "", "paper": PaperInput(title="T", doi="10.1000/z")}
    second = {"source": "crossref", "citations": 9, "venue": "NeurIPS",
              "paper": PaperInput(title="T", doi="10.1000/z", venue="NeurIPS", abstract="A2")}
    weaker = {"source": "arxiv", "citations": 2, "venue": "",
              "paper": PaperInput(title="T", doi="10.1000/z", abstract="A3")}
    merged, duplicates = merge_records([first, second, weaker])
    assert duplicates == 2 and len(merged) == 1
    entry = merged[0]
    assert entry["paper"].venue == "NeurIPS" and entry["paper"].abstract == "A2"
    assert entry["citations"] == 9 and entry["sources"] == ["openalex", "crossref", "arxiv"]


def test_one_failing_source_is_reported_and_does_not_stop_the_others():
    def broken(request):
        if request.url.host == "api.semanticscholar.org":
            return httpx.Response(429, content=b"slow down")
        return router(request)

    result = run(broken)
    assert len(result["documents"]) == 2
    assert result["source_failures"] == [{"source": "semanticscholar", "error": "提供商限流；稍后重试，不能据此认定资源未发布"}]
    assert "不代表论文不存在" in result["note"]


def test_every_source_failing_is_an_error_not_an_empty_result():
    def dead(request):
        return httpx.Response(500, content=b"upstream body that must not travel")

    with pytest.raises(ProviderError) as caught:
        run(dead)
    message = str(caught.value)
    assert "所有来源都未返回结果" in message and "HTTP 500" in message
    assert "upstream body" not in message and "这不代表论文不存在" in message


def test_a_source_returning_junk_is_reported_rather_than_crashing():
    def junk(request):
        if request.url.host == "api.openalex.org":
            return httpx.Response(200, content=b'{"results": [truncated mid-array',
                                 headers={"content-type": "application/json"})
        return router(request)

    result = run(junk)
    assert result["source_failures"] == [{"source": "openalex", "error": "提供商返回了无法解析的数据"}]
    assert len(result["documents"]) == 2


def test_an_html_interstitial_is_named_as_one_not_as_unparseable_data():
    """A 200 carrying an HTML page is an anti-scrape wall or a moved endpoint.

    Reporting it as "could not parse" would be technically true and practically useless: the
    reader needs to know the request was intercepted, not that the service sent odd JSON.
    """
    def wall(request):
        if request.url.host == "api.openalex.org":
            return httpx.Response(200, content=b"<!DOCTYPE html><html><body>Are you a robot?</body>",
                                  headers={"content-type": "text/html; charset=utf-8"})
        return router(request)

    result = run(wall)
    error = result["source_failures"][0]["error"]
    assert "HTML 页面" in error and "反爬" in error
    assert "无法解析的数据" not in error
    # The body is a remote-controlled string; it must not be echoed back to the caller.
    assert "Are you a robot" not in error
    assert len(result["documents"]) == 2


def test_each_key_is_only_sent_to_the_service_that_owns_it(monkeypatch):
    monkeypatch.setenv("OPENALEX_API_KEY", "openalex-secret-value")
    monkeypatch.setenv("OPENALEX_MAILTO", "someone@example.invalid")
    monkeypatch.setenv("SEMANTICSCHOLAR_API_KEY", "s2-secret-value")
    monkeypatch.delenv("OPENREVIEW_TOKEN", raising=False)
    seen = []

    def recorder(request):
        seen.append((request.url.host, str(request.url), dict(request.headers)))
        return router(request)

    result = run(recorder)
    by_host = {}
    for host, url, headers in seen:
        by_host.setdefault(host, (url, headers))
    openalex_url, openalex_headers = by_host["api.openalex.org"]
    assert "api_key=openalex-secret-value" in openalex_url
    assert "mailto=someone%40example.invalid" in openalex_url
    assert "Authorization" not in openalex_headers
    # The Semantic Scholar header name is the one its API documents.
    assert by_host["api.semanticscholar.org"][1].get("x-api-key") == "s2-secret-value"
    # Neither key travels to a host that does not own it.
    for host, (url, headers) in by_host.items():
        if host != "api.openalex.org":
            assert "openalex-secret-value" not in url
        if host != "api.semanticscholar.org":
            assert "s2-secret-value" not in json.dumps(headers)
    assert "openalex-secret-value" not in json.dumps(result) and "s2-secret-value" not in json.dumps(result)


def test_a_single_source_can_still_be_queried_alone():
    result = run(router, source="openalex")
    assert result["sources_queried"] == ["openalex"] and len(result["documents"]) == 1
    assert "note" not in result  # nothing to explain when only one source was asked


def test_an_unknown_year_is_not_filtered_out_but_a_known_one_is():
    assert in_year_range(None, 2020, 2024) is True
    assert in_year_range(2019, 2020, 2024) is False
    assert in_year_range(2024, 2020, 2024) is True
    assert in_year_range(2019, None, None) is True
    result = run(router, start_year=2024, end_year=2024)
    assert [document["paper"]["year"] for document in result["documents"]] == [2024]
    assert result["dropped_out_of_range"] == 1


def test_the_requested_year_window_reaches_the_source_apis():
    seen = {}

    def recorder(request):
        seen[request.url.host] = request.url.params
        return router(request)

    run(recorder, start_year=2020, end_year=2024)
    assert "from_publication_date:2020-01-01" in seen["api.openalex.org"]["filter"]
    assert "to_publication_date:2024-12-31" in seen["api.openalex.org"]["filter"]
    assert seen["api.semanticscholar.org"]["year"] == "2020-2024"
    assert "202001010000" in seen["export.arxiv.org"]["search_query"]
    assert seen["api.crossref.org"]["filter"] == "from-pub-date:2020-01-01,until-pub-date:2024-12-31"


def test_openalex_abstract_is_rebuilt_from_its_inverted_index():
    document = run(router, source="openalex")["documents"][0]
    assert "abstract: Layout matters here" in document["content"]


def test_a_reversed_year_window_is_rejected_by_the_contract():
    with pytest.raises(ValidationError):
        run(router, start_year=2025, end_year=2020)


def test_artifact_candidates_come_from_the_text_and_never_from_a_paper_venue():
    abstract = ("Code is at https://github.com/lab/paper, weights at "
                "https://huggingface.co/lab/weights, the record is "
                "https://arxiv.org/abs/2401.00001, and a blog is https://example.com/post.")
    assert artifact_urls(abstract) == ["https://github.com/lab/paper", "https://huggingface.co/lab/weights"]
    # Trailing sentence punctuation belongs to the sentence, not to the URL.
    assert artifact_urls("See https://github.com/lab/paper.") == ["https://github.com/lab/paper"]
    assert artifact_urls("") == [] and artifact_urls(None) == []
    assert artifact_urls("https://github.com/a/b https://github.com/c/d", limit=1) == ["https://github.com/a/b"]
    # No artifact host mentioned means no candidates, never a guessed search URL.
    assert artifact_urls("We release nothing. See https://doi.org/10.1000/x") == []
    assert artifact_urls("We release nothing at all.") == []


def test_an_arxiv_doi_yields_the_arxiv_id_so_the_primary_link_is_recoverable():
    # OpenAlex and Crossref often report only arXiv's DOI; the ID is encoded in it.
    assert arxiv_id_from_doi("10.48550/arXiv.2106.09685") == "2106.09685"
    assert arxiv_id_from_doi("10.48550/arxiv.2401.00001") == "2401.00001"
    assert arxiv_id_from_doi("10.1109/lsp.2024.3377590") == ""
    assert arxiv_id_from_doi("") == "" and arxiv_id_from_doi(None) == ""
    assert arxiv_id_from_doi("10.48550/arxiv.not-an-id") == ""


SKILL_SCRIPT = Path(__file__).resolve().parents[2] / "skills" / "re0-paper-search" / "scripts" / "paper_search.py"


def load_skill_module():
    """The retrieval logic lives in the package now, so this is the module under test."""
    from re0 import skill_search
    return skill_search


def load_skill_wrapper():
    """The skill file is a thin wrapper; it only has to locate the package and delegate."""
    spec = importlib.util.spec_from_file_location("paper_search_skill", SKILL_SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def observation():
    """A check as `github_check` really returns it: the file tree is itself evidence, so an
    audit conclusion can point at the listing it came from."""
    sha = "a" * 40
    return Observation(status="metadata_accessible", summary="仓库元数据可访问；扫描到 5 个文件条目。",
                       provider="github", depth="file_listing", scope="仅限公开元数据接口",
                       revision=sha, indicators={"training": ["train.py"]},
                       evidence=[Evidence(source_url=f"https://github.com/a/b/tree/{sha}",
                                          locator="GitHub Git Trees API", excerpt=f"commit={sha}"),
                                 Evidence(source_url=f"https://github.com/a/b/blob/{sha}/train.py",
                                          locator="train.py", excerpt="文件树中的候选路径：train.py",
                                          category="file_name_candidate")],
                       limitations=["仅检查默认分支文件名。"])


def skill_document(title, abstract):
    return {"paper": {"title": title, "abstract": abstract, "paper_url": "", "doi": "", "arxiv_id": ""},
            "locator": "fixture", "content": ""}


def test_open_source_candidates_reach_the_document_so_json_cannot_lose_them(capsys):
    """A report built from --json must not lose a feature's output.

    This was the gap that made the open-source search look absent: the candidates were printed for
    a human and never attached to the document, so anything reading the JSON saw nothing at all —
    indistinguishable from a tool that never had the feature.
    """
    module = load_skill_module()
    tools = StubTools({("search_repositories", None): [
        {"source_url": "https://github.com/360CVGroup/RevealLayer",
         "content": '{"full_name": "360CVGroup/RevealLayer"}'}]})
    document = skill_document(REVEAL_LAYER, "an abstract that carries no link at all")
    resource_audit.audit_document(document, tools, resource_audit.Budget(find=1))
    module.print_document(1, document, False)
    capsys.readouterr()
    assert document["artifact_search"] == "searched"
    assert document["artifact_candidates"] == [
        {"url": "https://github.com/360CVGroup/RevealLayer",
         "origin": "GitHub 名称检索·标识名与项目名一致"}]
    # An unverified candidate is still a row: dropping it would make "not checked" read as "checked
    # and found wanting", which is the confusion the audit states exist to prevent.
    assert document["artifact_outcome"] == "candidate_located"
    (row,) = document["resource_audits"]
    assert row["status"] == "not_checked" and row["verification_depth"] == "not_checked"
    # And the paper's own declared link is carried too, marked by where it came from.
    declared = skill_document("Other: A Paper", "Code at https://github.com/lab/other")
    resource_audit.audit_document(declared, None, resource_audit.Budget())
    module.print_document(1, declared, False)
    capsys.readouterr()
    assert declared["artifact_candidates"] == [{"url": "https://github.com/lab/other", "origin": "摘要中自述"}]
    # The authors pointing at their own repository is a declaration with a source, so it is recorded
    # as one rather than as a bare flag.
    (claim,) = declared["resource_audits"]
    assert claim["author_declaration"] == "released"
    assert claim["author_declaration_evidence"][0]["excerpt"] == "Code at https://github.com/lab/other"


def test_the_standing_rules_are_written_into_the_project_guidance():
    """These were asked for as standing rules, so they live in the repository rather than in a
    conversation that the next session cannot read."""
    root = Path(__file__).resolve().parents[2]
    agents = (root / "AGENTS.md").read_text(encoding="utf-8")
    assert "## Documentation and reporting discipline" in agents
    for phrase in ("surface a caller reads", "for completeness",
                   "Carry through what the tools reported"):
        assert phrase in agents, f"AGENTS.md lost: {phrase}"

    skill = (root / "skills" / "re0-paper-search" / "SKILL.md").read_text(encoding="utf-8")
    assert "## Reporting from these results" in skill
    for phrase in ("say how many you set aside", "artifact_candidates", "nobody searched them"):
        assert phrase in skill, f"SKILL.md lost: {phrase}"
    # What a layer can yield has to be stated next to the layer, or a name match reads as
    # authorship. It now lives in a reference, so the set is what gets checked.
    documents = skill_docs()
    assert "### What each layer can and cannot yield" in documents
    for phrase in ("no licence", "code but no weights", "can be a fork", "official attribution"):
        assert phrase in documents, f"the skill documentation lost the layer limit: {phrase}"


def test_the_retrieval_rules_reach_every_consumer_that_reads_them():
    """A rule only lands where its reader actually looks.

    The operative rules were once only in the CLI help, so the in-task model and MCP clients —
    which read the tool description and nothing else — never saw them, and the description had
    quietly gone stale on the merge rule. This pins all three surfaces together.
    """
    from re0.agent.tools import TOOL_TYPES

    description = TOOL_TYPES["search_papers"][1]
    assert "recall ceiling" in description, "the model cannot know limit bounds recall"
    assert "short queries" in description, "the model cannot know how to phrase a query"
    assert "every identifier" in description
    assert "DOI > arXiv ID > normalised title" not in description, "stale pre-fix merge rule"

    # MCP serves the same description, so a divergence here would split the two consumers.
    import re0.mcp_server as mcp_server
    catalog = {entry["name"]: entry["description"] for entry in mcp_server.catalog(web_enabled=False)}
    assert catalog["search_papers"] == description

    # The rules live in the package now, next to the code they describe.
    script = (Path(__file__).resolve().parents[2] / "backend" / "re0" / "skill_search.py").read_text(encoding="utf-8")
    assert "recall ceiling" in script and "Several short queries" in script
    # And the skill file delegates rather than keeping a second copy of the search logic.
    wrapper = (Path(__file__).resolve().parents[2] / "skills" / "re0-paper-search"
               / "scripts" / "paper_search.py").read_text(encoding="utf-8")
    assert "from re0.skill_search import main" in wrapper
    assert "def locate_backend()" in wrapper

    skill = (Path(__file__).resolve().parents[2] / "skills" / "re0-paper-search"
             / "SKILL.md").read_text(encoding="utf-8")
    assert "## Surveying a topic" in skill
    for phrase in ("three to five short queries", "--max-papers", "per-source hits",
                   "coverage.attempts", "A shared title is not enough to merge two works"):
        assert phrase in skill, f"the survey procedure lost: {phrase}"


def test_the_skill_locates_re0_whether_it_lives_in_the_repository_or_not(tmp_path, monkeypatch):
    module = load_skill_wrapper()
    repo = Path(__file__).resolve().parents[2]
    # Inside the repository: discovered from the script's own depth.
    monkeypatch.delenv("RE0_HOME", raising=False)
    assert module.locate_backend() == repo / "backend"
    # Anywhere else, an explicit checkout resolves the same way.
    monkeypatch.setenv("RE0_HOME", str(repo))
    assert module.locate_backend() == repo / "backend"
    # A wrong RE0_HOME fails loudly instead of silently falling back to something else.
    monkeypatch.setenv("RE0_HOME", str(tmp_path))
    with pytest.raises(SystemExit) as caught:
        module.locate_backend()
    assert caught.value.code == 2


def test_the_skill_declares_another_apps_directory_off_limits():
    # Installing next to other applications is supported, but writing into theirs is not:
    # the documentation has to say so, because it would silently change that agent.
    text = (Path(__file__).resolve().parents[2] / "skills" / "re0-paper-search" / "SKILL.md").read_text(encoding="utf-8")
    assert "another application's" in text and "never let this skill write to it" in text
    assert "name: re0-paper-search" in text


def test_a_venue_hint_is_prepended_and_stays_optional():
    """The hint is applied per source, by the tool, and stays optional.

    It used to be applied by the CLI before the request was built, which could not work once a
    real filter existed: whether a source can filter on a stable venue ID is only known after
    asking it. So the CLI passes the name through unchanged and the tool decides per source.
    """
    result = run(router, payload={"query": "diffusion watermarking", "limit": 5,
                                  "sources": ["arxiv"], "venue": "CVPR"})
    attempt = result["coverage"]["attempts"][0]
    assert attempt["query"] == "diffusion watermarking", "the caller's query is not rewritten"
    assert attempt["effective_query"] == "CVPR diffusion watermarking"
    row = result["coverage"]["venue_filter"]["per_source"][0]
    assert row["mode"] == "hint" and "不是严格过滤" in row["label"]

    plain = run(router, payload={"query": "diffusion watermarking", "limit": 5, "sources": ["arxiv"]})
    assert plain["coverage"]["attempts"][0]["effective_query"] == "diffusion watermarking"
    assert plain["coverage"]["venue_filter"]["requested"] == ""
    assert plain["coverage"]["venue_filter"]["per_source"] == []


def test_the_verification_budget_is_global_and_unchecked_links_are_still_listed(monkeypatch, capsys):
    module = load_skill_module()
    checked = []
    monkeypatch.setattr(resource_audit, "check_resource",
                        lambda url, transport=None: checked.append(url) or observation())
    documents = [skill_document("First", "Code at https://github.com/a/b"),
                 skill_document("Second", "Code at https://github.com/c/d")]
    budget = resource_audit.Budget(verify=1)
    for index, document in enumerate(documents, start=1):
        resource_audit.audit_document(document, None, budget)
        module.print_document(index, document, False)
    # The cap is global, not per paper: one check across two papers, never two.
    assert checked == ["https://github.com/a/b"]
    assert budget.verify_left == 0
    printed = capsys.readouterr().out
    # Both vocabularies survive: our state, and the provider status it was derived from. A 404 and
    # a 429 both land on `access_failed`, so dropping the provider's own value loses the difference.
    assert "状态 元数据可读（metadata_readable）；提供商状态 metadata_accessible" in printed
    assert "code_training=有候选" in printed
    # A link left unchecked is reported as a candidate, never as a failure.
    assert "开源候选（摘要中自述，未核验）: https://github.com/c/d" in printed
    # And the run reports its own denominator, so "1 verified" cannot be read as "1 exists".
    coverage = resource_audit.run_coverage(documents, budget)
    assert coverage["candidates"]["found"] == 2 and coverage["candidates"]["verified"] == 1
    assert coverage["candidates"]["unchecked"] == 1 and coverage["candidates"]["budget"] == 1
    assert coverage["audits"]["metadata_readable"] == 1 and coverage["audits"]["not_checked"] == 1


def test_a_check_that_raises_is_reported_as_unverified_not_as_absent(monkeypatch, capsys):
    module = load_skill_module()

    def explode(url, transport=None):
        raise RuntimeError("boom: /Users/someone/secret/path")

    monkeypatch.setattr(resource_audit, "check_resource", explode)
    document = skill_document("First", "Code at https://github.com/a/b")
    resource_audit.audit_document(document, None, resource_audit.Budget(verify=1))
    module.print_document(1, document, False)
    printed = capsys.readouterr().out
    assert "检查未完成" in printed
    # No traceback and no internal path in the output.
    assert "boom" not in printed and "Traceback" not in printed and "secret" not in printed
    # The data says the same thing: an exception is not an access failure of the resource, and it
    # is not "checked and absent" either. `provider_status` keeps the two reasons apart.
    (row,) = document["resource_audits"]
    assert row["status"] == "not_checked" and row["provider_status"] == "check_error"
    assert row["coverage"]["checkpoint"]["state"] == "unknown"


def test_env_file_parsing_is_conservative(tmp_path):
    path = tmp_path / ".env"
    path.write_text(
        "# a comment\n"
        "\n"
        "export QUOTED=\"value with spaces\"\n"
        "SINGLE='another value'\n"
        "EMPTY=\n"
        "PLAIN=abc\n"
        "NOT A NAME=x\n"
        "no_equals_here\n",
        encoding="utf-8")
    values = parse(path.read_text(encoding="utf-8"))
    assert values == {"QUOTED": "value with spaces", "SINGLE": "another value",
                      "EMPTY": "", "PLAIN": "abc"}
    assert load(tmp_path / "does-not-exist") == []


def test_env_file_never_shadows_a_real_variable(tmp_path, monkeypatch):
    monkeypatch.setenv("RE0_TEST_KEY", "from-environment")
    path = tmp_path / ".env"
    path.write_text("RE0_TEST_KEY=from-file\nRE0_TEST_OTHER=only-in-file\nEMPTY_ONE=\n", encoding="utf-8")
    assert load(path) == ["RE0_TEST_OTHER"]
    assert os.environ["RE0_TEST_KEY"] == "from-environment"
    assert load(path, override=True) == ["RE0_TEST_KEY", "RE0_TEST_OTHER"]
    assert os.environ["RE0_TEST_KEY"] == "from-file"


def test_the_document_carries_the_publication_label_the_ui_renders():
    """The card shows this label, so it is produced once on the server rather than copied into
    the frontend, where it would drift from the state vocabulary it describes."""
    result = run(router)
    published = next(item for item in result["documents"] if item["paper"]["title"] == "Fixture Layout Study")
    assert published["publication"]["state"] == "venue"
    assert published["publication"]["label"] == "有会议或期刊版本"
    # The raw venue stays alongside it, so a wrong classification is still checkable.
    assert published["publication"]["venue"] == "Neural Information Processing Systems"


def test_the_search_limit_can_be_raised_beyond_the_old_eight():
    # Recall is bounded by this cap, not by how many queries run. Eight made any survey a
    # matter of luck; conversation cost is bounded separately by the excerpt budget, so a
    # larger page per source is safe.
    assert PaperSearchArgs(query="x", limit=25).limit == 25
    with pytest.raises(ValidationError):
        PaperSearchArgs(query="x", limit=26)


def test_the_longest_abstract_wins_so_a_code_link_survives_the_merge():
    short = {"source": "a", "citations": None, "venue": "",
             "paper": PaperInput(title="Same Long Enough Title", abstract="No link here.")}
    long = {"source": "b", "citations": None, "venue": "",
            "paper": PaperInput(title="Same Long Enough Title",
                                abstract="We release code at https://github.com/lab/paper today.")}
    merged, duplicates = merge_records([short, long])
    assert duplicates == 1
    # First-source-wins would have kept the link-free abstract and lost the artifact scan.
    assert artifact_urls(merged[0]["paper"].abstract) == ["https://github.com/lab/paper"]


def test_a_failing_source_reports_the_credential_that_would_fix_it(monkeypatch):
    module = load_skill_module()
    monkeypatch.delenv("SEMANTIC_SCHOLAR_API_KEY", raising=False)
    monkeypatch.delenv("SEMANTICSCHOLAR_API_KEY", raising=False)
    assert "SEMANTIC_SCHOLAR_API_KEY" in module.failure_hint("semanticscholar")
    monkeypatch.setenv("SEMANTICSCHOLAR_API_KEY", "configured")
    assert module.failure_hint("semanticscholar") == ""   # the alias counts as configured
    assert module.failure_hint("crossref") == ""          # no credential exists for it


class StubTools:
    """Stands in for ResearchTools so the name search can be tested without any network."""

    def __init__(self, results):
        self.results, self.calls = results, []

    def execute(self, name, args, *, owner=""):
        self.calls.append((name, args.get("kind")))
        outcome = self.results.get((name, args.get("kind")))
        if isinstance(outcome, Exception):
            raise outcome
        return {"documents": outcome or []}


REVEAL_LAYER = ("RevealLayer: Disentangling Hidden and Visible Layers via "
                "Occlusion-Aware Image Decomposition")


def test_short_name_reads_the_project_name_a_paper_titles_itself_with():
    assert resource_audit.short_name(REVEAL_LAYER) == "RevealLayer"
    assert resource_audit.short_name("Stable-Layers: Fine-Tuning Image Layer Decomposition Models") == "Stable-Layers"
    assert resource_audit.short_name("UniWorld-Design: From Pixel Generation to Layer-Native Design") == "UniWorld-Design"
    # A sentence-like head is refused: searching a sentence only returns noise.
    assert resource_audit.short_name("Referring Layer Decomposition") == ""


def test_a_name_search_returns_candidates_with_a_confidence_marker():
    tools = StubTools({
        ("search_repositories", None): [{
            "source_url": "https://github.com/360CVGroup/RevealLayer",
            "content": '{"description": "RevealLayer: Disentangling Hidden and Visible Layers"}'}],
        ("search_hub", "models"): [{
            "source_url": "https://huggingface.co/qihoo360/RevealLayer",
            "content": '{"id": "qihoo360/RevealLayer"}'}],
        ("search_hub", "datasets"): [],
    })
    found = resource_audit.discover(skill_document(REVEAL_LAYER, "no link here"), tools)
    assert [candidate["url"] for candidate in found["candidates"]] == [
        "https://github.com/360CVGroup/RevealLayer", "https://huggingface.co/qihoo360/RevealLayer"]
    # A description that repeats the paper title is the strongest cheap signal; an identifier
    # ending in the project name is next. Neither proves authorship.
    assert "描述与论文标题相符" in found["candidates"][0]["origin"]
    assert "标识名与项目名一致" in found["candidates"][1]["origin"]
    assert found["failures"] == [] and found["state"] == "searched"
    assert found["project_name"] == "RevealLayer"


def test_a_fuzzy_name_match_is_marked_weak_and_sorted_last():
    # One query for "Stable-Layers" really does return these two, and the reader has to be able
    # to tell them apart from the official repository in the same list.
    tools = StubTools({
        ("search_repositories", None): [
            {"source_url": "https://github.com/snap-research/stable-flow",
             "content": '{"full_name": "snap-research/stable-flow", "description": null}'},
            {"source_url": "https://github.com/Stability-AI/Stable-Layers",
             "content": '{"full_name": "Stability-AI/Stable-Layers", "description": null}'},
            {"source_url": "https://github.com/nathannlu/aperture",
             "content": '{"full_name": "nathannlu/aperture", "description": null}'},
        ],
        ("search_hub", "models"): [],
        ("search_hub", "datasets"): [],
    })
    document = skill_document("Stable-Layers: Fine-Tuning Image Layer Decomposition Models", "")
    found = resource_audit.discover(document, tools)
    # The likely official repository is first even though the search returned it second.
    assert found["candidates"][0]["url"] == "https://github.com/Stability-AI/Stable-Layers"
    assert "标识名与项目名一致" in found["candidates"][0]["origin"]
    assert all("仅名称匹配" in candidate["origin"] for candidate in found["candidates"][1:])
    # And a name match never becomes an attribution on its own.
    resource_audit.audit_document(document, tools, resource_audit.Budget(find=1))
    assert all(row["attribution"] == "unconfirmed" for row in document["resource_audits"])


def test_a_failed_name_search_is_retried_and_never_reported_as_no_result(capsys):
    module = load_skill_module()
    calls = []

    class Flaky:
        def execute(self, name, args, *, owner=""):
            calls.append(name)
            raise ProviderError("网络连接或读取失败，本次未完成验证")

    document = skill_document("RevealLayer: Disentangling Hidden and Visible Layers",
                              "an abstract that carries no link at all")
    resource_audit.audit_document(document, Flaky(), resource_audit.Budget(find=1))
    module.print_document(1, document, False)
    printed = capsys.readouterr().out
    # A failure must not be reported as "found nothing": those are different claims.
    assert "检索未完成" in printed and "这不代表没有开源" in printed
    assert "也无结果" not in printed
    # Two attempts per endpoint, three endpoints.
    assert len(calls) == 6
    # The failure list reaches the document, so the JSON exit cannot lose it: without it a
    # total failure was indistinguishable from a search that completed and found nothing.
    assert document["artifact_search"] == "failed"
    assert len(document["artifact_search_detail"]["failures"]) == 3
    assert [item["ok"] for item in document["artifact_search_detail"]["endpoints"]] == [False] * 3
    assert document["artifact_outcome"] == "access_failed"


def test_a_disabled_name_search_is_not_reported_as_one_that_found_nothing(capsys):
    module = load_skill_module()
    document = skill_document(REVEAL_LAYER, "an abstract that carries no link at all")
    resource_audit.audit_document(document, StubTools({}), resource_audit.Budget(find=0))
    module.print_document(1, document, False)
    printed = capsys.readouterr().out
    assert "检索未开启" in printed
    assert "也无结果" not in printed
    assert document["artifact_search"] == "not-run"
    assert document["artifact_search_detail"]["reason_not_run"] == "disabled"


def test_the_artifact_line_prints_even_when_the_abstract_has_no_link(capsys):
    module = load_skill_module()
    document = skill_document("No links at all", "We release nothing in this paper.")
    resource_audit.audit_document(document, StubTools({}), resource_audit.Budget(find=1))
    module.print_document(1, document, False)
    printed = capsys.readouterr().out
    # An absent artifact module has to read as a finding, not as an omission.
    assert "开源线索: 摘要中未提及" in printed
    assert document["artifact_search"] == "skipped"
    assert document["artifact_outcome"] == "not_checked"

def test_the_cli_is_reachable_without_installing_a_console_script():
    """The console script only exists after an install, and an install needs a build backend. A
    checkout must still be usable, so the module entry point is what makes that true."""
    root = Path(__file__).resolve().parents[2]
    entry = root / "backend" / "re0" / "__main__.py"
    assert entry.is_file()
    assert "from re0.cli import main" in entry.read_text(encoding="utf-8")
    # The distribution declares the script too, so an install does provide `re0`.
    assert 're0 = "re0.cli:main"' in (root / "pyproject.toml").read_text(encoding="utf-8")


def test_the_documented_flag_contract_matches_the_parser():
    """A documented default that is not the real one is worse than no documentation, so the parser
    and the prose are held together here. Three drifts motivated this: prose said the name search
    covered 5 papers when the default is 10, the README said `--verify` took 0-5 when the cap is 8,
    and `--sources` was described as accepting a comma-separated subset while the code refuses more
    than one."""
    from re0 import skill_search
    from re0.agent.tools import TOOL_TYPES
    parser = skill_search.build_parser()
    options = {action.dest: action for action in parser._actions}
    assert options["find_artifacts"].default == 10
    assert options["verify"].default == 0
    assert options["max_papers"].default == 20
    assert options["sources"].default == "all"

    # argparse wraps the help to the terminal width, so the assertions run on a flattened copy.
    help_text = " ".join(parser.format_help().split())
    # `--sources` takes a subset now that the contract does; the old text promised exactly one.
    assert "comma-separated subset" in help_text
    assert "--queries" in help_text
    assert "default 10" in help_text and "tool maximum 25" in help_text

    root = Path(__file__).resolve().parents[2]
    readme = (root / "README.md").read_text(encoding="utf-8")
    skill = skill_docs()
    for prose in (readme, skill):
        assert "前 5 篇" not in prose and "first 5 papers" not in prose
        assert "0\u20135，默认 0" not in prose
    assert "默认对前 10 篇生效" in readme
    assert "first 10 papers" in skill

    # The audit surfaces. A rule that only reaches the CLI is invisible to the agent inside the
    # product, and a stale state table is worse than none: it reads as authority.
    assert "--resource-matrix" in help_text
    for prose in (readme, skill):
        assert "--resource-matrix" in prose
        # The vocabulary has to be spelled out wherever a reader decides what a status means.
        for state in ("not_found_in_scope", "access_required", "partially_available"):
            assert state in prose, f"a reader of this surface cannot interpret {state}"
        # And the two answers that must never be collapsed into one.
        assert "not_applicable" in prose and "unknown" in prose
    # The old vocabulary described the provider rather than the check; a table still keyed on it
    # would tell a reader that a 404 is "indeterminate" and an empty repository is a file count.
    assert "candidate files: training=" not in skill

    description = TOOL_TYPES["inspect_resource"][1]
    assert "not_found_in_scope" in description and "provider_status" in description
    assert "adapter" in description and "attribution" in description


def test_the_console_entry_point_keeps_no_model_and_byok_modes_apart(capsys):
    """`doctor` must say what runs without a key, must not probe the network unless asked, and
    must never read a credential value out."""
    from re0 import cli
    assert cli.main(["doctor"]) == 0
    printed = capsys.readouterr().out
    assert "need no model key" in printed and "standalone task does" in printed
    # `session` is split across both modes, and doctor has to say which half is which: a read command
    # that quietly needed a key would be the same trap the rest of this output exists to prevent.
    assert "session list/show/delta/scope" in printed
    assert "session follow-up" in printed
    assert "network probes: not run" in printed
    assert "--probe-network" in printed
    # Names and a yes/no only: a value must never be printed, not even masked.
    assert "RE0_LLM_API_KEY: not set" in printed
    assert "openssl" not in printed


def test_paper_search_forwards_its_flags_to_the_single_search_entry_point(capsys):
    """The console script and the skill wrapper must not diverge: both call re0.skill_search."""
    from re0 import cli, skill_search
    captured = {}

    def spy(argv=None):
        captured["argv"] = argv
        return 0

    original = skill_search.main
    skill_search.main = spy
    try:
        assert cli.main(["paper", "search", "--query", "layer decomposition", "--verify", "0"]) == 0
    finally:
        skill_search.main = original
    assert captured["argv"] == ["--query", "layer decomposition", "--verify", "0"]
    capsys.readouterr()


def test_an_unknown_command_is_refused_without_a_traceback(capsys):
    from re0 import cli
    assert cli.main(["nope"]) == 2
    assert "invalid choice" in capsys.readouterr().err
    assert cli.main([]) == 2
    capsys.readouterr()


def test_a_credential_file_named_explicitly_is_never_silently_replaced(monkeypatch, tmp_path):
    """Passing RE0_ENV_FILE means "use this file". Falling back to another one when it yields
    nothing is how a caller ends up authenticated as the wrong account."""
    from re0 import skill_search
    missing = tmp_path / "absent.env"
    monkeypatch.setenv("RE0_ENV_FILE", str(missing))
    reported = skill_search.load_credentials()
    assert str(missing) in reported and "supplied nothing" in reported

    credentials = tmp_path / "creds.env"
    credentials.write_text("GITHUB_TOKEN=placeholder-not-a-real-token\n", encoding="utf-8")
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.setenv("RE0_ENV_FILE", str(credentials))
    assert "1 variables set" in skill_search.load_credentials()
    # The value was applied to the process, and the report still names the file instead.
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)


def test_another_products_credential_file_is_not_read_unless_asked(monkeypatch):
    """Reading a different client's .env implicitly would pick up whichever account it held, so
    the default is to leave it alone and say so."""
    from re0 import skill_search
    monkeypatch.delenv("RE0_ENV_FILE", raising=False)
    monkeypatch.delenv("RE0_ENV_INCLUDE_AGENT_DIRS", raising=False)
    assert "~/.codex/skills/.env" not in skill_search.ENV_FILES
    assert skill_search.AGENT_ENV_FILES == ("~/.codex/skills/.env",)
    reported = skill_search.load_credentials()
    assert "another product" in reported or "ambient environment" in reported


def test_the_query_and_source_contract_accepts_one_or_several():
    """The single-query form has to keep working, and several has to be possible in one call."""
    assert PaperSearchArgs(query="x").queries_effective() == ["x"]
    assert PaperSearchArgs(queries=["a", " b "]).queries_effective() == ["a", "b"]
    assert PaperSearchArgs(query="x", source="arxiv").sources_effective() == ["arxiv"]
    assert PaperSearchArgs(queries=["a"], sources=["arxiv", "crossref"]).sources_effective() == \
        ["arxiv", "crossref"]
    with pytest.raises(ValidationError):
        PaperSearchArgs(queries=[])
    with pytest.raises(ValidationError):
        PaperSearchArgs(queries=["a"] * 6)
    with pytest.raises(ValidationError):
        PaperSearchArgs(queries=["ok", "   "])


def test_several_queries_run_in_one_call_and_each_record_keeps_which_query_found_it():
    """Hand-merging JSON files loses this: the record carries every query that surfaced it, and a
    work found by both queries is one record rather than two."""
    result = run(router, queries=["layout", "fixture layout"])
    assert result["queries"] == ["layout", "fixture layout"]
    assert result["coverage"]["requested"]["queries"] == ["layout", "fixture layout"]
    # Every (query, source) pair is an attempt, not only the last round's.
    pairs = {(item["query"], item["source"]) for item in result["coverage"]["attempts"]}
    assert ("layout", "openalex") in pairs and ("fixture layout", "openalex") in pairs
    assert len(result["coverage"]["attempts"]) == 10
    merged = next(item for item in result["documents"]
                  if item["paper"]["title"] == "Fixture Layout Study")
    assert "queries: layout, fixture layout" in merged["content"]
    assert result["coverage"]["state"] == "ok"


def test_coverage_separates_zero_hits_from_a_partial_run():
    """An empty answer and a broken source are different states, and a list length cannot tell
    them apart."""

    def empty_world(request):
        if request.url.host == "api.openalex.org":
            return httpx.Response(200, json={"results": []})
        return httpx.Response(200, json={"data": []})

    zero = run(empty_world, source="openalex")
    assert zero["coverage"]["state"] == "zero_hits"
    assert zero["coverage"]["hits"]["records"] == 0 and not zero["coverage"]["failed"]
    assert zero["source_counts"] == {"openalex": 0}

    def openalex_down(request):
        if request.url.host == "api.openalex.org":
            return httpx.Response(500, content=b"<html>upstream detail that must not travel</html>")
        if request.url.host == "api.semanticscholar.org":
            return httpx.Response(200, json=SEMANTIC)
        return httpx.Response(404)

    partial = run(openalex_down, sources=["openalex", "semanticscholar"])
    assert partial["coverage"]["state"] == "partial"
    assert [item["source"] for item in partial["coverage"]["failed"]] == ["openalex"]
    # The same failure sits at the top level, where callers already read it.
    assert partial["source_failures"] == partial["coverage"]["failed"]
    assert partial["coverage"]["succeeded"] == ["semanticscholar"]
    # A partial run still returns what did arrive, and does not present the gap as absence.
    assert partial["documents"]
    assert "upstream detail" not in json.dumps(partial)


def test_a_shared_title_does_not_merge_two_different_works():
    """A title is the weakest key we match on. Two works that disagree on real identifiers stay two
    records: merging them would mint a paper that does not exist."""
    first = paper_record("openalex", PaperInput(title="A Shared And Sufficiently Long Title",
                                                doi="10.1000/alpha", year=2020))
    second = paper_record("crossref", PaperInput(title="A Shared And Sufficiently Long Title",
                                                 doi="10.1000/beta", year=2022))
    merged, duplicates = merge_records([first, second])
    assert duplicates == 0 and len(merged) == 2


def test_a_preprint_and_its_published_version_still_merge_and_both_dois_survive():
    """The conservative case: an arXiv DOI beside a publisher DOI is the same work, so it merges,
    and neither identifier is dropped."""
    preprint = paper_record("arxiv", PaperInput(title="A Shared And Sufficiently Long Title",
                                                doi="10.48550/arXiv.2401.00001", year=2024))
    published = paper_record("crossref", PaperInput(title="A Shared And Sufficiently Long Title",
                                                    doi="10.1000/real", year=2024))
    merged, duplicates = merge_records([preprint, published])
    assert duplicates == 1 and len(merged) == 1
    # DOIs are case-insensitive by definition, so the normalised form is the correct one to keep.
    assert merged[0]["paper"].doi.lower() == "10.48550/arxiv.2401.00001"
    # The second identifier is kept rather than overwritten, so the record stays checkable.
    assert merged[0]["other_dois"] == ["10.1000/real"]


def test_merge_records_accumulates_the_queries_that_found_a_work():
    first = paper_record("openalex", PaperInput(title="A Shared And Sufficiently Long Title",
                                                doi="10.1000/same"))
    first["queries"] = ["layer decomposition"]
    second = paper_record("arxiv", PaperInput(title="A Shared And Sufficiently Long Title",
                                              doi="10.1000/same"))
    second["queries"] = ["layered image generation"]
    merged, duplicates = merge_records([first, second])
    assert duplicates == 1
    assert merged[0]["queries"] == ["layer decomposition", "layered image generation"]


# --- #5-B: bounded pagination, shared budgets, provider deferrals, venue filters ------------

import re0.agent.tools as tools_module  # noqa: E402  (after the fixtures it patches)

OPENALEX_PAGE1 = {"results": OPENALEX["results"], "meta": {"count": 2, "next_cursor": "cursor-2"}}
OPENALEX_PAGE2 = {"results": [{"id": "https://openalex.org/W2", "title": "A Second Page Record",
                               "publication_year": 2023,
                               "primary_location": {"source": {"display_name": "FixtureConf"}}}],
                  "meta": {"count": 2, "next_cursor": None}}
OPENALEX_SOURCES = {"results": [
    {"id": "https://openalex.org/S4363607701", "type": "conference",
     "display_name": "2022 IEEE/CVF Conference on Computer Vision and Pattern Recognition (CVPR)"}]}


def paged_openalex(request):
    if request.url.host == "api.openalex.org" and request.url.path == "/works":
        cursor = request.url.params.get("cursor")
        return httpx.Response(200, json=OPENALEX_PAGE1 if cursor in (None, "*") else OPENALEX_PAGE2)
    return router(request)


@pytest.fixture
def slept(monkeypatch):
    """Replace the governor's sleep so a Retry-After window is recorded without being waited."""
    waits = []
    real = tools_module.Governor

    def factory(**kwargs):
        kwargs["sleep"] = waits.append
        return real(**kwargs)

    monkeypatch.setattr(tools_module, "Governor", factory)
    return waits


def pages_of(result, source):
    return [row for row in result["coverage"]["pagination"] if row["source"] == source]


def test_a_documented_source_walks_pages_until_the_cursor_runs_out():
    result = run(paged_openalex, payload={"query": "layout", "limit": 5,
                                          "sources": ["openalex"], "max_pages": 4})
    row = pages_of(result, "openalex")[0]
    assert (row["pages_fetched"], row["stop_reason"], row["truncated"]) == (2, "complete", False)
    assert row["pagination_documented"] is True and row["records"] == 2
    assert "A Second Page Record" in [document["paper"]["title"] for document in result["documents"]]


def test_the_page_ceiling_stops_the_walk_and_keeps_the_cursor_visible():
    result = run(paged_openalex, payload={"query": "layout", "limit": 5,
                                          "sources": ["openalex"], "max_pages": 1})
    row = pages_of(result, "openalex")[0]
    assert (row["pages_fetched"], row["stop_reason"], row["truncated"]) == (1, "page_budget", True)
    assert row["next_cursor"] == "cursor-2", "where the walk stopped has to be resumable"
    assert "A Second Page Record" not in [d["paper"]["title"] for d in result["documents"]]


def test_a_source_without_documented_paging_says_so_in_the_coverage():
    result = run(router, payload={"query": "layout", "limit": 5,
                                  "sources": ["arxiv", "crossref"], "max_pages": 5})
    for source in ("arxiv", "crossref"):
        row = pages_of(result, source)[0]
        assert row["stop_reason"] == "not_supported" and row["pagination_documented"] is False
        assert row["truncated"] is True and row["pages_fetched"] == 1
        assert "未分页不等于已查全" in row["detail"]


def test_the_shared_request_ceiling_is_never_exceeded_and_the_stop_is_reported():
    result = run(router, payload={"query": "layout", "limit": 5,
                                  "sources": ["openalex", "crossref", "openreview"],
                                  "max_requests": 1})
    scheduling = result["coverage"]["scheduling"]
    assert scheduling["requests_used"] == 1 == scheduling["max_requests"]
    assert result["coverage"]["state"] == "partial"
    assert [failure["source"] for failure in result["source_failures"]] == ["crossref", "openreview"]
    assert "共享请求预算" in result["source_failures"][0]["error"]
    # The page that was never requested is recorded as a budget stop, not as a provider failure:
    # one is this run's own limit, the other is the service's answer, and they are not the same.
    assert pages_of(result, "crossref")[0]["stop_reason"] == "request_budget"
    assert pages_of(result, "openreview")[0]["stop_reason"] == "request_budget"


def test_a_rate_limit_is_waited_out_using_the_providers_own_retry_after(slept):
    seen = {"n": 0}

    def flaky(request):
        if request.url.host == "api.openalex.org":
            seen["n"] += 1
            if seen["n"] == 1:
                return httpx.Response(429, headers={"Retry-After": "3"}, content=b"slow down")
            return httpx.Response(200, json=OPENALEX)
        return router(request)

    result = run(flaky, payload={"query": "layout", "limit": 5, "sources": ["openalex"]})
    assert slept == [3.0], "the provider's own instruction sets the wait"
    assert result["coverage"]["scheduling"]["deferred_seconds"] == {"api.openalex.org": 3.0}
    assert result["coverage"]["scheduling"]["retries"] == {}, "a success clears the retry count"
    assert len(result["documents"]) == 1, "the retry recovered the page rather than losing it"


def test_an_exhausting_rate_limit_is_a_failure_of_the_source_not_an_empty_result(slept):
    def blocked(request):
        if request.url.host == "api.openalex.org":
            return httpx.Response(429, headers={"Retry-After": "1"}, content=b"slow down")
        return router(request)

    # Two sources, so the run survives and its coverage can be read: when every source fails the
    # tool raises instead of returning an empty list, which its own test pins separately.
    result = run(blocked, payload={"query": "layout", "limit": 5,
                                   "sources": ["openalex", "crossref"]})
    assert len(slept) == 2, "bounded: one retry, not an unbounded loop"
    assert result["coverage"]["state"] == "partial"
    assert pages_of(result, "openalex")[0]["stop_reason"] == "rate_limited"
    assert "限流" in result["source_failures"][0]["error"]
    assert result["coverage"]["scheduling"]["deferred_seconds"] == {"api.openalex.org": 2.0}
    assert pages_of(result, "crossref")[0]["stop_reason"] == "not_supported"


def test_a_repeat_inside_one_session_is_served_from_the_cache_and_refresh_is_not():
    calls = {"n": 0}

    def counting(request):
        if request.url.host == "api.openalex.org":
            calls["n"] += 1
        return router(request)

    tools = ResearchTools(None, httpx.MockTransport(counting))
    payload = {"query": "layout", "limit": 5, "sources": ["openalex"]}
    first = tools.execute("search_papers", dict(payload))
    assert first["coverage"]["scheduling"]["cache"]["hits"] == 0
    second = tools.execute("search_papers", dict(payload))
    assert calls["n"] == 1, "the same question in one session is not asked twice"
    assert second["coverage"]["scheduling"]["cache"]["hits"] == 1
    third = tools.execute("search_papers", dict(payload, refresh=True))
    assert calls["n"] == 2, "refresh re-fetches instead of trusting the TTL"
    cache = third["coverage"]["scheduling"]["cache"]
    assert cache["refreshed"] == 1 and cache["hits"] == 1, "a forced refresh is not counted as a hit"
    assert "会话累计" in cache["note"], "the counters outlive the call and must say so"


def test_a_cache_built_anonymously_is_not_reused_once_a_key_is_configured(monkeypatch):
    """The entitlement is part of the cache key, in both directions.

    Serving an anonymous answer to a credentialed call would under-report; serving a credentialed
    answer to an anonymous one would disclose records the caller is not entitled to see.
    """
    calls = {"n": 0}

    def counting(request):
        if request.url.host == "api.openalex.org":
            calls["n"] += 1
        return router(request)

    monkeypatch.delenv("OPENALEX_API_KEY", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    tools = ResearchTools(None, httpx.MockTransport(counting))
    payload = {"query": "layout", "limit": 5, "sources": ["openalex"]}
    tools.execute("search_papers", dict(payload))
    tools.execute("search_papers", dict(payload))
    assert calls["n"] == 1
    monkeypatch.setenv("OPENALEX_API_KEY", "openalex-secret-value")
    result = tools.execute("search_papers", dict(payload))
    assert calls["n"] == 2, "a changed entitlement invalidates the entry"
    assert result["coverage"]["scheduling"]["cache_scope"] == "openalex"
    # The counters are the session's, so the earlier anonymous hit is still in them; what proves
    # the point is that the credentialed call was a miss and had to go back to the network.
    assert result["coverage"]["scheduling"]["cache"]["hits"] == 1
    assert result["coverage"]["scheduling"]["cache"]["stores"] == 2


def test_a_venue_resolves_to_a_stable_source_id_and_the_names_it_matched_are_shown():
    seen = []

    def venue_router(request):
        if request.url.host == "api.openalex.org" and request.url.path == "/sources":
            seen.append(("sources", request.url.params.get("filter")))
            return httpx.Response(200, json=OPENALEX_SOURCES)
        if request.url.host == "api.openalex.org" and request.url.path == "/works":
            seen.append(("works", request.url.params.get("filter"), request.url.params.get("search")))
            return httpx.Response(200, json=OPENALEX)
        return router(request)

    result = run(venue_router, payload={"query": "layer decomposition", "limit": 5,
                                        "sources": ["openalex"], "venue": "CVPR"})
    assert seen[0] == ("sources", "display_name.search:CVPR")
    assert "primary_location.source.id:S4363607701" in seen[1][1]
    assert seen[1][2] == "layer decomposition", "a strict filter does not also pollute the query"
    row = result["coverage"]["venue_filter"]["per_source"][0]
    assert row["mode"] == "strict" and row["resolved_id"] == "S4363607701"
    assert row["resolved_names"] == [OPENALEX_SOURCES["results"][0]["display_name"]]
    assert "只覆盖" in row["detail"], "a family split into editions must not look fully covered"


def test_a_venue_that_does_not_resolve_falls_back_to_a_hint_and_says_it_is_one():
    def venue_router(request):
        if request.url.host == "api.openalex.org" and request.url.path == "/sources":
            return httpx.Response(200, json={"results": []})
        if request.url.host == "api.openalex.org" and request.url.path == "/works":
            seen.append(request.url.params.get("filter", ""))
            searches.append(request.url.params.get("search"))
            return httpx.Response(200, json=OPENALEX)
        return router(request)

    seen, searches = [], []
    result = run(venue_router, payload={"query": "layer decomposition", "limit": 5,
                                        "sources": ["openalex"], "venue": "NoSuchVenue"})
    assert "primary_location.source.id" not in seen[0]
    assert searches[0] == "NoSuchVenue layer decomposition"
    row = result["coverage"]["venue_filter"]["per_source"][0]
    assert row["mode"] == "resolve_failed" and "更宽而不是更窄" in row["detail"]


def test_a_source_without_a_verified_venue_filter_gets_a_hint_and_is_labelled_a_hint():
    result = run(router, payload={"query": "layer decomposition", "limit": 5,
                                  "sources": ["semanticscholar", "arxiv"], "venue": "CVPR"})
    modes = {row["source"]: row["mode"] for row in result["coverage"]["venue_filter"]["per_source"]}
    assert modes == {"semanticscholar": "hint", "arxiv": "hint"}
    assert "不是严格过滤" in result["coverage"]["venue_filter"]["per_source"][0]["label"]
    attempts = {row["source"]: row for row in result["coverage"]["attempts"]}
    assert attempts["arxiv"]["effective_query"] == "CVPR layer decomposition"
    assert attempts["arxiv"]["query"] == "layer decomposition", "the original query stays visible"


def test_a_venue_name_that_matches_an_unrelated_source_is_not_used_as_a_filter():
    """`display_name.search` is a relevance search, so a hit has to be checked, not trusted."""
    def venue_router(request):
        if request.url.host == "api.openalex.org" and request.url.path == "/sources":
            return httpx.Response(200, json={"results": [
                {"id": "https://openalex.org/S1", "display_name": "Some Unrelated Journal"}]})
        return paged_openalex(request)

    result = run(venue_router, payload={"query": "layout", "limit": 5,
                                        "sources": ["openalex"], "venue": "CVPR"})
    row = result["coverage"]["venue_filter"]["per_source"][0]
    assert row["mode"] == "resolve_failed" and row["resolved_id"] == ""


def test_every_attempt_keeps_its_own_pagination_row_across_queries_and_sources():
    result = run(paged_openalex, payload={"queries": ["layer", "decomposition"], "limit": 5,
                                          "sources": ["openalex", "arxiv"], "max_pages": 2})
    rows = result["coverage"]["pagination"]
    assert {(row["query"], row["source"]) for row in rows} == {
        ("layer", "openalex"), ("layer", "arxiv"),
        ("decomposition", "openalex"), ("decomposition", "arxiv")}
    assert all(row["query"] in row["effective_query"] for row in rows)


def test_the_scheduling_block_names_the_entitlement_without_naming_a_secret(monkeypatch):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.setenv("OPENALEX_API_KEY", "openalex-secret-value")
    result = run(router, payload={"query": "layout", "limit": 5, "sources": ["openalex"]})
    scheduling = result["coverage"]["scheduling"]
    assert scheduling["cache_scope"] == "openalex"
    assert "openalex-secret-value" not in json.dumps(result, ensure_ascii=False, default=str)
