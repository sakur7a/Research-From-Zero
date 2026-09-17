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

from re0.agent.tools import ResearchTools
from re0.env_file import load, parse
from re0.literature import (arxiv_id_from_doi, artifact_urls, classify_venue, finalize_publication,
                            in_year_range, merge_records, paper_keys, paper_record)
from re0.models import Observation, PaperInput
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
    assert "publication: 已收录于会议或期刊 — Neural Information Processing Systems" in published["content"]
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
            return httpx.Response(200, content=b"<html>not json</html>")
        return router(request)

    result = run(junk)
    assert result["source_failures"] == [{"source": "openalex", "error": "提供商返回了无法解析的数据"}]
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


SKILL_SCRIPT = Path(__file__).resolve().parents[2] / "skills" / "paper-search" / "scripts" / "paper_search.py"


def load_skill_module():
    spec = importlib.util.spec_from_file_location("paper_search_skill", SKILL_SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def observation():
    return Observation(status="metadata_accessible", summary="仓库元数据可访问；扫描到 5 个文件条目。",
                       provider="github", depth="file_listing", scope="仅限公开元数据接口",
                       indicators={"training": ["train.py"]}, limitations=["仅检查默认分支文件名。"])


def skill_document(title, abstract):
    return {"paper": {"title": title, "abstract": abstract, "paper_url": "", "doi": "", "arxiv_id": ""},
            "locator": "fixture", "content": ""}


def test_the_verification_budget_is_global_and_unchecked_links_are_still_listed(monkeypatch, capsys):
    module = load_skill_module()
    checked = []
    monkeypatch.setattr(module, "check_resource", lambda url: checked.append(url) or observation())
    documents = [skill_document("First", "Code at https://github.com/a/b"),
                 skill_document("Second", "Code at https://github.com/c/d")]
    budget = 1
    for index, document in enumerate(documents, start=1):
        budget -= module.print_document(index, document, False, budget)
    # The cap is global, not per paper: one check across two papers, never two.
    assert checked == ["https://github.com/a/b"]
    printed = capsys.readouterr().out
    assert "status metadata_accessible" in printed and "candidate files: training=1" in printed
    # A link left unchecked is reported as a candidate, never as a failure.
    assert "unverified): https://github.com/c/d" in printed


def test_a_check_that_raises_is_reported_as_unverified_not_as_absent(monkeypatch, capsys):
    module = load_skill_module()

    def explode(url):
        raise RuntimeError("boom: /Users/someone/secret/path")

    monkeypatch.setattr(module, "check_resource", explode)
    module.print_document(1, skill_document("First", "Code at https://github.com/a/b"), False, 1)
    printed = capsys.readouterr().out
    assert "unverified" in printed
    # No traceback and no internal path in the output.
    assert "boom" not in printed and "Traceback" not in printed and "secret" not in printed


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
