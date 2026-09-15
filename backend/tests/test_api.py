import json

from re0.main import create_app
from fastapi.testclient import TestClient


def paper(client, **kwargs):
    response = client.post("/api/papers", json={"title": "A study", **kwargs})
    assert response.status_code == 201, response.text
    return response.json()


def resource(client, paper_id, **kwargs):
    response = client.post(f"/api/papers/{paper_id}/resources", json={"kind": "code", "label": "Implementation", "url": "https://example.org/research", **kwargs})
    assert response.status_code == 201, response.text
    return response.json()


def test_health_and_initial_empty_library(client):
    assert client.get("/api/health").json()["llm_enabled"] is False
    assert client.get("/api/papers").json() == []
    assert len(client.get("/api/topics").json()) == 3


def test_create_update_get_and_delete(client):
    p = paper(client, authors=["A", "B"], topics=["Layout", "Layers"], status="reading", notes="A note")
    assert p["notes"] == "A note"
    assert p["topics"] == ["Layout", "Layers"]
    assert p["is_demo"] is False
    result = client.put(f"/api/papers/{p['id']}", json={"title": "Revised", "notes": "Private notes", "status": "baseline"})
    assert result.status_code == 200
    assert client.get(f"/api/papers/{p['id']}").json()["status"] == "baseline"
    assert client.delete(f"/api/papers/{p['id']}").status_code == 204
    assert client.get(f"/api/papers/{p['id']}").status_code == 404


def test_duplicate_doi_never_overwrites_notes(client):
    paper(client, doi="https://doi.org/10.1234/ABC", notes="Keep this")
    response = client.post("/api/papers", json={"title": "B", "doi": "10.1234/abc", "notes": "Overwrite"})
    assert response.status_code == 409
    assert client.get("/api/papers").json()[0]["notes"] == "Keep this"


def test_arxiv_versions_share_work_identity(client):
    paper(client, arxiv_id="1706.03762v1")
    assert client.post("/api/papers", json={"title": "v2", "arxiv_id": "1706.03762v2"}).status_code == 409


def test_update_to_duplicate_identity_is_transactional(client):
    first = paper(client, doi="10.1234/one")
    second = paper(client, title="Keep", doi="10.1234/two")
    assert client.put(f"/api/papers/{second['id']}", json={"title": "Do not save", "doi": first["doi"]}).status_code == 409
    assert client.get(f"/api/papers/{second['id']}").json()["title"] == "Keep"


def test_claims_require_evidence(client):
    p = paper(client)
    base = {"kind": "code", "label": "Repository", "url": "https://github.com/example/repo"}
    assert client.post(f"/api/papers/{p['id']}/resources", json={**base, "ownership": "official"}).status_code == 422
    assert client.post(f"/api/papers/{p['id']}/resources", json={**base, "claim": "released"}).status_code == 422
    r = resource(client, p["id"], ownership="official", ownership_evidence="Paper p.3 links this repo")
    assert r["ownership"] == "official"


def test_unsupported_check_is_saved_without_network_or_claim_changes(client):
    p = paper(client, arxiv_id="1706.03762v1", notes="Keep secret")
    r = resource(client, p["id"], ownership="official", ownership_evidence="User assertion")
    checked = client.post(f"/api/resources/{r['id']}/check", json={})
    assert checked.status_code == 200
    obs = checked.json()["observation"]
    assert obs["status"] == "unsupported"
    assert obs["depth"] == "not_verified"
    assert obs["paper_version_snapshot"]["arxiv_id"] == "1706.03762v1"
    assert "Keep secret" not in json.dumps(obs)
    listed = client.get(f"/api/papers/{p['id']}").json()["resources"][0]
    assert listed["ownership"] == "official"
    assert listed["latest"]["status"] == "unsupported"


def test_check_cache_and_append_only_history(client):
    p = paper(client)
    r = resource(client, p["id"])
    path = f"/api/resources/{r['id']}/check"
    first = client.post(path, json={}).json()
    second = client.post(path, json={}).json()
    assert first["cached"] is False
    assert second["cached"] is True
    assert len(client.get(f"/api/resources/{r['id']}/observations").json()) == 1
    store = client.app.state.store
    old = {**first["observation"], "checked_at": "2000-01-01T00:00:00+00:00", "status": "access_failed"}
    store.save_observation(r["id"], old)
    assert client.post(path, json={}).json()["cached"] is False
    history = client.get(f"/api/resources/{r['id']}/observations").json()
    assert len(history) == 3
    assert history[1]["status"] == "access_failed"


def test_cascading_resource_and_observation_deletion(client):
    p = paper(client)
    r = resource(client, p["id"])
    client.post(f"/api/resources/{r['id']}/check", json={})
    client.delete(f"/api/papers/{p['id']}")
    assert client.get(f"/api/resources/{r['id']}/observations").status_code == 404
    with client.app.state.store.db.connect() as con:
        assert con.execute("SELECT COUNT(*) FROM observations").fetchone()[0] == 0


def test_demo_explicit_idempotent_and_isolated(client):
    real = paper(client, notes="Must stay")
    assert client.post("/api/demo", json={}).json()["created"] == 6
    assert client.post("/api/demo", json={}).json()["created"] == 0
    demos = [p for p in client.get("/api/papers").json() if p["is_demo"]]
    assert len(demos) == 6
    r = next(p["resources"][0] for p in demos if p["resources"])
    assert client.post(f"/api/resources/{r['id']}/check", json={}).status_code == 409
    assert client.delete("/api/demo").json()["deleted"] == 6
    assert client.get(f"/api/papers/{real['id']}").json()["notes"] == "Must stay"


def test_csl_preview_then_partial_import_and_dedup(client):
    items = [{"title": "Valid paper", "DOI": "10.1234/valid", "author": [{"given": "A", "family": "B"}], "issued": {"date-parts": [[2025]]}},
             {"title": "Duplicate", "DOI": "10.1234/valid"}, {"title": ""}]
    preview = client.post("/api/import/csl", json={"items": items}).json()
    assert preview["ready"] == 1
    assert len(preview["errors"]) == 1
    assert len(preview["skipped"]) == 1
    assert client.get("/api/papers").json() == []
    result = client.post("/api/import/csl", json={"items": items, "dry_run": False}).json()
    assert len(result["created"]) == 1
    assert client.get("/api/papers").json()[0]["authors"] == ["A B"]
    repeated = client.post("/api/import/csl", json={"items": items, "dry_run": False}).json()
    assert repeated["created"] == []


def test_csl_invalid_structure_and_large_import(client):
    assert client.post("/api/import/csl", json={"items": [{}] * 501}).status_code == 422
    assert client.post("/api/import/csl", json={"items": ["bad"]}).status_code == 422
    bad = [{"title": "Bad URL", "URL": "javascript:alert(1)"}, {"title": "Bad author", "author": ["x"]}]
    assert len(client.post("/api/import/csl", json={"items": bad}).json()["errors"]) == 2


def test_export_contains_evidence_and_safe_bibtex(client):
    p = paper(client, title="{x} & 100%", notes="Export me")
    r = resource(client, p["id"])
    client.post(f"/api/resources/{r['id']}/check", json={})
    data = client.get("/api/export").json()
    assert data["schema_version"] == 1
    assert data["papers"][0]["resources"][0]["observations"][0]["evidence"]
    bib = client.get("/api/export?format=bibtex")
    assert bib.status_code == 200
    assert r"\{x\} \& 100\%" in bib.text
    assert "Export me" not in bib.text


def test_security_headers_and_no_cors(client):
    response = client.get("/")
    assert response.status_code == 200
    assert "frame-ancestors 'none'" in response.headers["content-security-policy"]
    assert response.headers["x-content-type-options"] == "nosniff"
    assert "access-control-allow-origin" not in client.get("/api/papers", headers={"Origin": "https://evil.example"}).headers


def test_write_origin_and_client_header_guards(client):
    assert client.post("/api/papers", json={"title": "x"}, headers={"X-Re0-Client": "wrong"}).status_code == 403
    assert client.post("/api/papers", json={"title": "x"}, headers={"Origin": "https://evil.example"}).status_code == 403
    assert client.post("/api/papers", json={"title": "x"}, headers={"Origin": "http://testserver"}).status_code == 201
    assert client.post("/api/papers", content="title=x", headers={"Content-Type": "text/plain"}).status_code == 415
    assert client.get("/api/papers", headers={"Host": "evil.example"}).status_code == 400


def test_request_size_and_extra_fields_rejected(client):
    assert client.post("/api/papers", content=b"x" * (4*1024*1024+1)).status_code == 413
    assert client.post("/api/papers", json={"title": "x", "is_demo": True}).status_code == 422


def test_invalid_urls_and_data_rejected(client):
    for url in ["javascript:alert(1)", "file:///etc/passwd", "https://user:pass@github.com/a/b", "https://x/\nInjected"]:
        assert client.post("/api/papers", json={"title": "x", "paper_url": url}).status_code == 422
    assert client.post("/api/papers", json={"title": "   "}).status_code == 422
    assert client.post("/api/papers", json={"title": "x", "year": 9999}).status_code == 422
    assert client.post("/api/papers", json={"title": "x", "topics": [" "]}).status_code == 422


def test_sqlite_persists_between_app_instances(tmp_path):
    path = str(tmp_path / "persist.sqlite3")
    with TestClient(create_app(path), headers={"X-Re0-Client": "web"}) as first:
        first.post("/api/papers", json={"title": "Persistent"})
    with TestClient(create_app(path)) as second:
        assert second.get("/api/papers").json()[0]["title"] == "Persistent"


def test_invalid_metadata_identifier_does_not_fetch(client):
    result = client.post("/api/metadata/resolve", json={"identifier": "http://127.0.0.1/private"})
    assert result.status_code == 422
