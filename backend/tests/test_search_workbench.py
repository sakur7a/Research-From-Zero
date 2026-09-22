"""The retrieval workbench's contract with the backend it reads from.

`web/search.html` renders the file that `re0 paper search --json` writes. It adds no endpoint and no
second retrieval implementation, so what has to be pinned is the seam: the fixture the Node tests run
against must still be what the real code path produces, and the labels the page shows must still be
the ones `models.py` defines. Either one drifting silently is how a page comes to describe a result
differently from the terminal that produced it.
"""
import importlib.util
import json
import pathlib
import re

import httpx
from fastapi.testclient import TestClient

from re0.main import create_app
from re0.models import AUDIT_LABELS, COMPONENT_LABELS

ROOT = pathlib.Path(__file__).resolve().parents[2]
WEB = ROOT / "web"
FIXTURE = ROOT / "tests" / "fixtures" / "search-result.json"


def generator():
    path = ROOT / "scripts" / "gen_search_fixture.py"
    spec = importlib.util.spec_from_file_location("gen_search_fixture", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def js_table(name: str) -> dict:
    text = (WEB / "search-core.js").read_text(encoding="utf-8")
    body = re.search(rf"export const {name} = \{{(.*?)\}};", text, re.S).group(1)
    return dict(re.findall(r"(\w+):\s*'([^']*)'", body))


def test_the_checked_in_fixture_is_what_the_backend_really_writes():
    """Regenerated through `paper_document`, `ResourceAudit`, `run_coverage` and `normalize`.

    If this fails, the page's Node tests are passing against a shape the product no longer produces —
    which is worse than no tests, because they keep passing.
    """
    assert json.loads(FIXTURE.read_text(encoding="utf-8")) == generator().build()


def test_the_fixture_says_it_is_fictional():
    """Four invented papers with invented DOIs sit in a file a page renders; it has to label itself."""
    structure = json.loads(FIXTURE.read_text(encoding="utf-8"))
    assert "虚构" in structure["note"]
    for document in structure["documents"]:
        doi = document["paper"]["doi"]
        assert not doi or doi.startswith("10.99999/"), doi


def test_the_page_shows_the_backend_labels_and_not_its_own():
    assert js_table("AUDIT_LABELS") == AUDIT_LABELS
    assert js_table("COMPONENT_LABELS") == COMPONENT_LABELS


def test_the_page_does_not_keep_a_second_copy_of_the_publication_labels():
    """`paper_document` puts the label in the payload precisely so no UI has to keep one."""
    text = (WEB / "search-core.js").read_text(encoding="utf-8")
    assert "PUBLICATION_LABELS = {" not in text
    assert "publication.label" in text


def test_a_paper_document_exposes_its_sources_and_citations_as_fields():
    """Both were only in the prose body, so every consumer had to parse a sentence to get them."""
    from re0.agent.tools import paper_document
    from re0.models import PaperInput

    record = {"paper": PaperInput(title="A Study", doi="10.99999/fixture"),
              "sources": ["crossref", "openalex"], "citations": 12, "publication": {},
              "institutions": ["Some University"]}
    document = paper_document(record)
    assert document["sources"] == ["crossref", "openalex"]
    assert document["citations"] == 12
    from re0 import result_model
    normalized = result_model.normalize({"documents": [document]})["documents"][0]
    assert normalized["sources"] == ["crossref", "openalex"] and normalized["citations"] == 12
    # A citation count nobody reported stays absent rather than becoming a zero.
    assert paper_document({**record, "citations": None})["citations"] is None


def test_the_normalized_structure_keeps_the_window_that_was_requested():
    structure = json.loads(FIXTURE.read_text(encoding="utf-8"))
    assert structure["queries"] == ["layer decomposition", "layered generation"]
    assert structure["coverage"]["requested"]["start_year"] == 2023
    assert structure["coverage"]["state"] == "partial"
    assert len(structure["coverage"]["attempts"]) == 3


def test_the_page_is_served_by_the_static_mount_that_already_exists(tmp_path):
    """No route was added for this page, and that is the point worth pinning."""
    app = create_app(str(tmp_path / "workbench.sqlite3"),
                     httpx.MockTransport(lambda request: None))
    with TestClient(app) as client:
        for name in ("search.html", "search.js", "search-core.js", "search.css"):
            response = client.get(f"/static/{name}")
            assert response.status_code == 200, name
        assert "检索工作台" in client.get("/static/search.html").text


def test_both_existing_pages_link_to_the_workbench():
    """A page nobody can reach is a page nobody will notice is stale."""
    assert "/static/search.html" in (WEB / "agent.html").read_text(encoding="utf-8")
    assert "/static/search.html" in (WEB / "app.js").read_text(encoding="utf-8")


def test_the_page_adds_no_backend_surface_of_its_own():
    """It reads a file and, on an explicit click, posts to an endpoint that already existed."""
    text = (WEB / "search.js").read_text(encoding="utf-8")
    calls = set(re.findall(r"api\('([^']+)'", text))
    assert calls == {"/import/resource-audits"}, calls
