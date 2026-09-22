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


AUDIT_ROW = {
    "paper_title": "LayerKit: A Study",
    "work_identifier": "2401.00001",
    "work_version": "arXiv 2401.00001",
    "resource_url": "https://github.com/lab/layerkit",
    "resource_type": "code",
    "candidate_origin": "摘要中自述",
    "author_declaration": "released",
    "author_declaration_evidence": [{"source_url": "https://arxiv.org/abs/2401.00001",
                                     "locator": "论文摘要", "excerpt": "Code is at the repository.",
                                     "category": "author_declaration"}],
    "status": "metadata_readable",
    "provider_status": "metadata_accessible",
    "provider": "github",
    "summary": "仓库元数据可访问。",
    "access": "open",
    "scope": "默认分支文件树",
    "revision": "a" * 40,
    "checked_at": "2026-09-22T00:00:00+00:00",
    "verification_depth": "file_listing",
    "coverage": {"code_training": {"state": "present", "paths": ["train.py"],
                                   "sources": ["https://github.com/lab/layerkit/blob/"
                                               + "a" * 40 + "/train.py"]}},
    "licences": {"code": "apache-2.0"},
    "limitations": ["仅检查文件名。"],
}
AUDIT_ITEM = {"paper": {"title": "LayerKit: A Study", "arxiv_id": "2401.00001", "authors": ["A"]},
              "audits": [AUDIT_ROW]}


def test_a_check_stores_the_field_level_audit_beside_the_raw_observation(client):
    """The library's history has to answer "which artifact classes does this resource cover", not
    only "what did the provider say". Both are stored, and the older fields keep their meaning."""
    p = paper(client)
    r = resource(client, p["id"], url="https://example.org/not-a-provider")
    observation = client.post(f"/api/resources/{r['id']}/check", json={}).json()["observation"]
    assert observation["status"] == "unsupported"
    audit = observation["resource_audit"]
    assert audit["status"] == "unsupported" and audit["verification_depth"] == "not_checked"
    # An unsupported link was never checked, so it claims nothing about any artifact class.
    assert {name: item["state"] for name, item in audit["coverage"].items()} == {
        name: "unknown" for name in audit["coverage"]}
    assert observation["record_kind"] == "observation"


def test_a_human_confirmation_is_its_own_record_and_never_rewrites_the_observation(client):
    p = paper(client)
    # An unsupported link, so the check stores an observation without any network request.
    r = resource(client, p["id"])
    client.post(f"/api/resources/{r['id']}/check", json={})
    revised = {**AUDIT_ROW, "resource_url": r["url"], "attribution": "official",
               "attribution_evidence": [{"source_url": "https://arxiv.org/abs/2401.00001",
                                         "locator": "论文第 3 页", "excerpt": "代码发布于该地址。"}]}
    created = client.post(f"/api/resources/{r['id']}/confirmations", json=revised)
    assert created.status_code == 201, created.text
    history = client.get(f"/api/resources/{r['id']}/observations").json()
    kinds = [row["record_kind"] for row in history]
    # The confirmation is appended; the check it revises is still there to be read back.
    assert kinds == ["confirmation", "observation"]
    assert history[0]["attribution"] == "official" and history[0]["record_origin"] == "user"
    assert history[1]["resource_audit"]["attribution"] == "unconfirmed"
    assert history[1]["record_origin"] == "check"


def test_a_confirmation_without_a_source_or_about_another_resource_is_refused(client):
    p = paper(client)
    r = resource(client, p["id"], url="https://github.com/lab/layerkit")
    # Settling an attribution is exactly the claim that needs a source behind it, and the refusal
    # says so: the reader was asked for evidence and did not attach any.
    bare = client.post(f"/api/resources/{r['id']}/confirmations",
                       json={**AUDIT_ROW, "attribution": "official"})
    assert bare.status_code == 422 and "交叉证据" in bare.json()["detail"]
    elsewhere = client.post(f"/api/resources/{r['id']}/confirmations",
                            json={**AUDIT_ROW, "resource_url": "https://github.com/other/repo"})
    assert elsewhere.status_code == 422 and "对应这个资源" in elsewhere.json()["detail"]
    assert client.get(f"/api/resources/{r['id']}/observations").json() == []


def test_an_audit_import_previews_then_links_idempotently_without_touching_notes(client):
    existing = paper(client, arxiv_id="2401.00001", notes="我自己的笔记")
    preview = client.post("/api/import/resource-audits", json={"items": [AUDIT_ITEM]})
    assert preview.status_code == 200
    body = preview.json()
    assert body["dry_run"] is True and body["ready"] == 1 and body["linked"] == []
    assert body["preview"][0]["resources"] == ["https://github.com/lab/layerkit"]

    applied = client.post("/api/import/resource-audits",
                          json={"items": [AUDIT_ITEM], "dry_run": False}).json()
    # The paper already existed, so it is linked rather than recreated, and the notes survive.
    assert applied["created"] == []
    (link,) = applied["linked"]
    assert link["paper_id"] == existing["id"]
    assert client.get(f"/api/papers/{existing['id']}").json()["notes"] == "我自己的笔记"

    stored = client.get(f"/api/papers/{existing['id']}").json()
    (imported,) = stored["resources"]
    assert imported["url"] == "https://github.com/lab/layerkit"
    # The audit's declaration is transcribed, not re-judged: same vocabulary, evidence kept.
    assert imported["claim"] == "released" and "论文摘要" in imported["claim_evidence"]
    assert imported["ownership"] == "unconfirmed"
    (observation,) = client.get(f"/api/resources/{imported['id']}/observations").json()
    assert observation["record_origin"] == "import" and observation["record_kind"] == "observation"

    again = client.post("/api/import/resource-audits",
                        json={"items": [AUDIT_ITEM], "dry_run": False}).json()
    # One resource, one observation: a second approval of the same check adds nothing.
    assert again["linked"] == [] and len(again["skipped"]) == 1
    assert "已导入过" in again["skipped"][0]["reason"]
    assert len(client.get(f"/api/papers/{existing['id']}").json()["resources"]) == 1
    assert len(client.get(f"/api/resources/{imported['id']}/observations").json()) == 1


def test_an_import_refuses_a_record_no_source_supports(client):
    # An audit with no rows, a paper with no title, and an attribution with no evidence are all
    # refusals rather than invented library records.
    response = client.post("/api/import/resource-audits", json={"items": [
        {"paper": {"title": "No audits"}, "audits": []},
        {"paper": {"title": ""}, "audits": [AUDIT_ROW]},
        {"paper": {"title": "Settled"}, "audits": [{**AUDIT_ROW, "attribution": "official"}]},
        {"paper": {"title": "Unsupported link"},
         "audits": [{**AUDIT_ROW, "resource_url": "https://zenodo.org/records/1",
                     "resource_type": "unknown"}]},
    ], "dry_run": False})
    assert response.status_code == 200
    body = response.json()
    assert len(body["errors"]) == 3 and body["ready"] == 1
    # The fourth parses but its link was never checkable, so it is reported and creates nothing:
    # an import that cannot attach a resource must not leave a bare paper behind.
    assert body["created"] == [] and body["linked"] == []
    assert body["skipped"] == [{"index": 3, "url": "https://zenodo.org/records/1",
                                "reason": "链接类型不受支持，未产生审计结论"}]
    assert client.get("/api/papers").json() == []


def test_an_observation_recorded_before_records_were_labelled_still_reads_back(client):
    """v0.2 data has no `record_kind`, and it was all written by the only writer that existed."""
    p = paper(client)
    r = resource(client, p["id"])
    store = client.app.state.store
    with store.db.connect() as con:
        con.execute("INSERT INTO observations(resource_id,data,checked_at) VALUES (?,?,?)",
                    (r["id"], json.dumps({"status": "metadata_accessible",
                                          "checked_at": "2026-01-01T00:00:00+00:00"}),
                     "2026-01-01T00:00:00+00:00"))
    (row,) = client.get(f"/api/resources/{r['id']}/observations").json()
    assert row["status"] == "metadata_accessible"
    assert row["record_kind"] == "observation"


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
