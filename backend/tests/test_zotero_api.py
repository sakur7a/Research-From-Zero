"""The HTTP surface for Zotero sync: the same service the console calls, wrapped by the API.

These tests exist because the service-level suite cannot see the parts that only an endpoint can get
wrong — a credential echoed back in a response or a validation error, a scope that is narrowed
between the request body and the wire, and a disconnect that quietly takes papers with it.
"""
import httpx
from fastapi.testclient import TestClient

from re0.deployment import LOCAL_OWNER
from re0.main import create_app
from re0.models import PaperInput
from test_zotero import KEY, ZoteroService, item

HEADERS = {"X-Re0-Client": "web", "Content-Type": "application/json"}
EDITABLE = ("title", "authors", "year", "venue", "abstract", "doi", "arxiv_id", "paper_url",
            "topics", "status", "version_label")


def _app(tmp_path, service):
    return TestClient(create_app(str(tmp_path / "api.sqlite3"), httpx.MockTransport(service)),
                      headers=HEADERS)


def _body(**extra):
    return {"library_type": "user", "library_id": "12345", "api_key": KEY,
            "label": "fixture 用户库", **extra}


def _status(client):
    return client.get("/api/zotero/status",
                      params={"library_type": "user", "library_id": "12345"}).json()


def _json_requests(service):
    return [request for request in service.requests
            if request.url.params.get("format") == "json"]


def test_the_http_sync_previews_by_default_and_commits_only_when_asked(tmp_path):
    service = ZoteroService(items=[item("AAAA0001", 300, "通过 HTTP 同步进来的论文",
                                        doi="10.1000/http")], library_version=300)
    with _app(tmp_path, service) as client:
        preview = client.post("/api/zotero/sync", json=_body())
        assert preview.status_code == 200, preview.text
        body = preview.json()
        assert body["applied"] is False and body["counts"]["added"] == 1
        assert "没有写入任何内容" in body["note"]
        assert client.get("/api/papers").json() == []
        assert _status(client)["cursor"]["committed_version"] == 0

        applied = client.post("/api/zotero/sync", json=_body(apply=True))
        assert applied.status_code == 200, applied.text
        assert applied.json()["applied"] is True
        papers = client.get("/api/papers").json()
        assert len(papers) == 1 and papers[0]["doi"] == "10.1000/http"
        status = _status(client)
        assert status["cursor"]["committed_version"] == 300
        assert status["links"] == {"linked": 1} and status["distinct_papers"] == 1
        assert status["recent_syncs"][0]["applied"] is True

        # A second sync over an unchanged library is still a no-op, and duplicates nothing.
        again = client.post("/api/zotero/sync", json=_body(apply=True)).json()
        assert again["counts"]["added"] == 0 and again["counts"]["unchanged"] == 1
        assert len(client.get("/api/papers").json()) == 1


def test_no_endpoint_echoes_the_zotero_key(tmp_path):
    service = ZoteroService(items=[item("AAAA0001", 300, "标题", doi="10.1000/a")],
                            library_version=300)
    with _app(tmp_path, service) as client:
        responses = [client.post("/api/zotero/sync", json=_body(apply=True)),
                     client.post("/api/zotero/collections", json=_body()),
                     client.get("/api/zotero/status",
                                params={"library_type": "user", "library_id": "12345"}),
                     client.get("/api/zotero/links")]
        for response in responses:
            assert response.status_code == 200, response.text
            assert KEY not in response.text
        # A refused body must not echo the credential either: the app-wide handler replaces every
        # validation message precisely because these payloads carry secrets.
        for bad_body in ({**_body(), "unknown_field": 1}, {**_body(), "library_id": ""},
                         {**_body(), "library_type": "robots"}):
            bad = client.post("/api/zotero/sync", json=bad_body)
            assert bad.status_code == 422, bad.text
            assert KEY not in bad.text
        with client.app.state.store.db.connect() as con:
            assert KEY not in "\n".join(con.iterdump())


def test_a_provider_failure_is_reported_without_leaking_the_request(tmp_path):
    def refusing(request):
        return httpx.Response(403, json={"message": "Forbidden", "key": KEY})

    with _app(tmp_path, refusing) as client:
        response = client.post("/api/zotero/sync", json=_body())
        assert response.status_code == 502
        assert KEY not in response.text
        assert client.get("/api/papers").json() == []
        assert _status(client)["cursor"]["committed_version"] == 0


def test_collection_selection_is_stored_and_narrows_the_next_sync(tmp_path):
    service = ZoteroService(items=[item("AAAA0001", 300, "标题", doi="10.1000/a",
                                        collections=["COLL0001"])], library_version=300)
    with _app(tmp_path, service) as client:
        listed = client.post("/api/zotero/collections", json=_body())
        assert listed.status_code == 200, listed.text
        assert [row["key"] for row in listed.json()["collections"]] == ["COLL0001"]
        assert listed.json()["collections"][0]["name"] == "图层分解"

        selected = client.put("/api/zotero/selection",
                              json={"library_type": "user", "library_id": "12345",
                                    "collections": ["COLL0001"]})
        assert selected.status_code == 200 and selected.json()["selected"] == ["COLL0001"]
        unknown = client.put("/api/zotero/selection",
                             json={"library_type": "user", "library_id": "12345",
                                   "collections": ["NOSUCH01"]})
        assert unknown.status_code == 404

        # The stored selection is used when the request names none, and it reaches the wire.
        result = client.post("/api/zotero/sync", json=_body()).json()
        assert result["scope"]["collections"] == ["COLL0001"]
        requests = _json_requests(service)
        assert requests and requests[-1].url.params.get("collection") == "COLL0001"
        assert _status(client)["collections"][0]["selected"] is True


def test_several_collections_are_all_sent_and_tags_are_reported_as_a_union(tmp_path):
    service = ZoteroService(items=[], library_version=10)
    with _app(tmp_path, service) as client:
        result = client.post("/api/zotero/sync", json=_body(
            collections=["COLL0001", "COLL0002", "COLL0003"], tags=["甲", "乙"])).json()
        # Nothing is quietly dropped: three collections in, three collections on the wire.
        assert result["scope"]["collections"] == ["COLL0001", "COLL0002", "COLL0003"]
        assert result["scope"]["tag_mode"] == "union"
        assert any("并集" in note for note in result["notes"])
        requests = _json_requests(service)
        assert requests
        assert requests[-1].url.params.get("collection") == "COLL0001,COLL0002,COLL0003"
        assert requests[-1].url.params.get("tag") == "甲,乙"
        # What is stored on the cursor is the scope that was applied, so `status` cannot go on to
        # describe a different sync than the one that ran. A repeated key is deduplicated before the
        # request and must not reappear in the record.
        applied = client.post("/api/zotero/sync", json=_body(
            collections=["COLL0001", "COLL0001", "COLL0002"], tags=["甲"], apply=True)).json()
        assert applied["scope"]["collections"] == ["COLL0001", "COLL0002"]
        assert _json_requests(service)[-1].url.params.get("collection") == "COLL0001,COLL0002"
        assert _status(client)["cursor"]["scope"] == applied["scope"]


def test_an_unscoped_sync_says_that_it_read_the_whole_library(tmp_path):
    service = ZoteroService(items=[], library_version=10)
    with _app(tmp_path, service) as client:
        result = client.post("/api/zotero/sync", json=_body()).json()
        assert any("整个库" in note for note in result["notes"])
        requests = _json_requests(service)
        assert requests and "collection" not in requests[-1].url.params
        assert "tag" not in requests[-1].url.params


def test_the_item_request_never_asks_for_a_private_type_over_http(tmp_path):
    service = ZoteroService(items=[item("AAAA0001", 300, "标题", doi="10.1000/a")],
                            library_version=300)
    with _app(tmp_path, service) as client:
        client.post("/api/zotero/sync", json=_body(apply=True))
    requests = _json_requests(service)
    assert requests
    asked = requests[-1].url.params.get("itemType", "").split("||")
    assert "journalArticle" in asked
    for private in ("attachment", "note", "annotation"):
        assert private not in asked


def test_disconnect_over_http_keeps_every_paper_and_can_drop_only_the_mappings(tmp_path):
    service = ZoteroService(items=[item("AAAA0001", 300, "断开后仍应留下的论文",
                                        doi="10.1000/a")], library_version=300)
    with _app(tmp_path, service) as client:
        client.post("/api/zotero/sync", json=_body(apply=True))
        paper = client.get("/api/papers").json()[0]
        revised = client.put(f"/api/papers/{paper['id']}", json={
            **{key: paper[key] for key in EDITABLE}, "notes": "HTTP 断开连接也不能删掉这条"})
        assert revised.status_code == 200, revised.text

        soft = client.post("/api/zotero/disconnect",
                           json={"library_type": "user", "library_id": "12345"}).json()
        assert soft["removed_links"] == 0 and soft["papers_remaining"] == 1
        assert client.get("/api/zotero/links").json()
        assert _status(client)["cursor"]["committed_version"] == 0

        hard = client.post("/api/zotero/disconnect",
                           json={"library_type": "user", "library_id": "12345",
                                 "remove_links": True}).json()
        assert hard["removed_links"] == 1 and hard["papers_remaining"] == 1
        assert client.get("/api/zotero/links").json() == []
        kept = client.get("/api/papers").json()[0]
        assert kept["notes"] == "HTTP 断开连接也不能删掉这条"
        assert kept["title"] == "断开后仍应留下的论文"


def test_a_write_to_the_zotero_endpoints_needs_the_client_header(tmp_path):
    service = ZoteroService(items=[], library_version=10)
    with _app(tmp_path, service) as client:
        anonymous = client.post("/api/zotero/sync", json=_body(),
                                headers={"Content-Type": "application/json",
                                         "X-Re0-Client": "something-else"})
        assert anonymous.status_code == 403
        # Reads need no header, and must work without a credential anywhere.
        assert client.get("/api/zotero/status",
                          params={"library_type": "user", "library_id": "12345"}).status_code == 200
        assert client.get("/api/papers").json() == []


def test_a_synced_paper_still_takes_part_in_the_ordinary_library(tmp_path):
    """A Zotero-sourced paper is a library paper: resources, observations and export all still work."""
    service = ZoteroService(items=[item("AAAA0001", 300, "同步进来的论文", doi="10.1000/a")],
                            library_version=300)
    with _app(tmp_path, service) as client:
        client.post("/api/zotero/sync", json=_body(apply=True))
        paper = client.get("/api/papers").json()[0]
        from re0.models import ResourceInput
        created = client.post(f"/api/papers/{paper['id']}/resources",
                              json=ResourceInput(kind="code", label="repo",
                                                 url="https://github.com/owner/repo").model_dump())
        assert created.status_code == 201, created.text
        assert len(client.get(f"/api/papers/{paper['id']}").json()["resources"]) == 1
        assert len(client.get("/api/zotero/links", params={"paper_id": paper["id"]}).json()) == 1
        exported = client.get("/api/export").json()
        assert any(row["doi"] == "10.1000/a" for row in exported["papers"])
        assert KEY not in client.get("/api/export").text
        # The library's own duplicate guard still applies to a synced DOI.
        from fastapi import HTTPException
        try:
            client.app.state.store.create_paper(PaperInput(title="重复 DOI", doi="10.1000/a"),
                                                    owner=LOCAL_OWNER)
            raise AssertionError("a duplicate DOI was accepted")
        except HTTPException as exc:
            assert exc.status_code == 409


def test_the_surfaces_a_caller_reads_say_what_the_scope_filters_now_do():
    """Pin the descriptions, not only the behaviour.

    `--collection` promised one collection and `--tag` promised five ANDed, while the connector
    sends every collection in one request and every tag as a union. A help text that understates a
    filter makes a reader sync three times to cover what one call covers; the README paragraph that
    called remote sync unimplemented was worse, because it told a reader the feature did not exist.
    """
    import contextlib
    import io
    from pathlib import Path

    from re0.zotero_cli import main

    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        assert main(["sync", "--library-id", "12345", "--help"]) == 0
    help_text = " ".join(buffer.getvalue().split())
    assert "all of them are sent in one request" in help_text, help_text
    assert "matched as a union" in help_text, help_text
    for stale in ("to one collection", "up to five", "ANDs"):
        assert stale not in help_text, f"CLI help still says: {stale}"

    root = Path(__file__).resolve().parents[2]
    readme = (root / "README.md").read_text(encoding="utf-8")
    assert "远端 API 同步尚未实现" not in readme
    for surface in ("/api/zotero/sync", "/api/zotero/status", "并集"):
        assert surface in readme, f"README does not document {surface}"
    # The dialog that replaced that paragraph must not carry the claim back in.
    assert "远端 API 同步尚未实现" not in (root / "web" / "app.js").read_text(encoding="utf-8")
    changelog = (root / "CHANGELOG.md").read_text(encoding="utf-8")
    assert "Not done in this increment:** the HTTP endpoints" not in changelog
