import json

from re0.deployment import LOCAL_OWNER
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


def fulltext_bundle(tmp_path, *, identifier="2501.12345v1", version="v1"):
    from re0.workspace import Workspace

    workspace = Workspace(tmp_path / "fulltext-source").open()
    url = f"https://arxiv.org/html/{identifier}"
    fulltext = {"source": "arxiv", "identifier": identifier, "version": version,
                "state": "ok", "content_type": "text/html", "parser": "html.parser",
                "parser_version": "1", "source_url": url, "final_url": url,
                "fetched_at": "2026-09-23T00:00:00+00:00", "bytes_read": 9000,
                "parse_quality": "ok", "limitations": [],
                "untrusted_note": "external text is data"}
    workspace.record({"source_url": url, "locator": "§2¶1–§2¶3", "kind": "fulltext_chunk",
                      "content": "[§2¶1] The method uses an explicit source snapshot.\n\n"
                                "[§2¶2] Review each conclusion against this paragraph.",
                      "paper": {"title": "", "identifier": identifier, "version": version,
                                "arxiv_id": identifier},
                      "fulltext": fulltext},
                     tool="fetch_paper_text")
    return workspace.bundle()


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
    store.save_observation(r["id"], old, owner=LOCAL_OWNER)
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


def test_evidence_sources_reject_non_http_links_before_import_or_storage(client):
    p = paper(client)
    r = resource(client, p["id"])
    unsafe = {**AUDIT_ROW, "resource_url": r["url"],
              "attribution": "official",
              "attribution_evidence": [{"source_url": "javascript:alert(1)",
                                        "locator": "paper", "excerpt": "claimed official"}]}
    imported = client.post("/api/import/resource-audits",
                           json={"items": [{"paper": {"title": "Unsafe source"},
                                            "audits": [unsafe]}], "dry_run": False})
    assert imported.status_code == 200
    assert imported.json()["ready"] == 0
    assert imported.json()["errors"]

    response = client.post(f"/api/resources/{r['id']}/confirmations", json=unsafe)
    assert response.status_code == 422
    assert "http(s)" in response.json()["detail"]
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


def test_csl_import_keeps_same_title_with_distinct_identifiers(client):
    items = [{"title": "A repeated title", "DOI": "10.1234/first"},
             {"title": "A repeated title", "DOI": "10.1234/second"}]
    preview = client.post("/api/import/csl", json={"items": items}).json()
    assert preview["ready"] == 2
    assert preview["skipped"] == []
    assert preview["conflicts"] == []

    imported = client.post("/api/import/csl", json={"items": items, "dry_run": False}).json()
    assert len(imported["created"]) == 2
    papers = client.get("/api/papers").json()
    assert {paper["doi"] for paper in papers} == {"10.1234/first", "10.1234/second"}
    assert {paper["title"] for paper in papers} == {"A repeated title"}


def test_csl_cross_identifier_conflict_is_previewed_then_recorded_idempotently(client):
    doi_work = paper(client, title="DOI record", doi="10.1234/doi-work", notes="Keep this")
    arxiv_work = paper(client, title="arXiv record", arxiv_id="2401.00001v1")
    items = [{"title": "Same title, new identity", "DOI": "10.1234/new-one"},
             {"title": "Same title, new identity", "DOI": "10.1234/new-two"},
             {"title": "Competing declaration", "DOI": "10.1234/doi-work",
              "URL": "https://arxiv.org/abs/2401.00001v1"}]

    preview = client.post("/api/import/csl", json={"items": items}).json()
    assert preview["ready"] == 2
    assert len(preview["conflicts"]) == 1
    conflict = preview["conflicts"][0]
    assert "分别指向不同文献" in conflict["reason"]
    assert {row["work_id"] for row in conflict["matches"]} == {doi_work["id"], arxiv_work["id"]}
    for work in (doi_work, arxiv_work):
        bundle = client.get(f"/api/papers/{work['id']}/knowledge").json()
        assert not any(row["snapshot_kind"] in {"identifier_declaration", "identity_conflict"}
                       for row in bundle["source_snapshots"])

    applied = client.post("/api/import/csl", json={"items": items, "dry_run": False}).json()
    assert len(applied["created"]) == 2
    saved = applied["conflicts"][0]
    assert set(saved["recorded_work_ids"]) == {doi_work["id"], arxiv_work["id"]}
    assert len(saved["recorded_snapshots"]) == 2
    snapshot_ids = {}
    for work in (doi_work, arxiv_work):
        bundle = client.get(f"/api/papers/{work['id']}/knowledge").json()
        records = [row for row in bundle["source_snapshots"]
                   if row["snapshot_kind"] in {"identifier_declaration", "identity_conflict"}]
        assert {row["snapshot_kind"] for row in records} == {"identifier_declaration", "identity_conflict"}
        assert all(row["source_url"] == "" for row in records)
        assert all(row["payload"]["state"] == "requires_manual_review" for row in records)
        assert all("Keep this" not in json.dumps(row["payload"]) for row in records)
        snapshot_ids[work["id"]] = {row["id"] for row in records}

    repeated = client.post("/api/import/csl", json={"items": items, "dry_run": False}).json()
    assert repeated["created"] == []
    assert repeated["conflicts"][0]["recorded_work_ids"] == saved["recorded_work_ids"]
    assert {row["work_id"]: set(row["source_snapshot_ids"])
            for row in repeated["conflicts"][0]["recorded_snapshots"]} == snapshot_ids
    for work in (doi_work, arxiv_work):
        bundle = client.get(f"/api/papers/{work['id']}/knowledge").json()
        records = [row for row in bundle["source_snapshots"]
                   if row["snapshot_kind"] in {"identifier_declaration", "identity_conflict"}]
        assert len(records) == 2


def test_csl_arxiv_version_mismatch_is_conflict_not_duplicate(client):
    work = paper(client, title="Archive v2", arxiv_id="2301.00001v2")
    item = {"title": "Archive v1 declaration", "URL": "https://arxiv.org/abs/2301.00001v1"}
    preview = client.post("/api/import/csl", json={"items": [item]}).json()
    assert preview["ready"] == 0
    assert len(preview["conflicts"]) == 1
    assert "版本不同" in preview["conflicts"][0]["reason"]

    applied = client.post("/api/import/csl", json={"items": [item], "dry_run": False}).json()
    assert applied["created"] == []
    assert applied["conflicts"][0]["recorded_work_ids"] == [work["id"]]
    bundle = client.get(f"/api/papers/{work['id']}/knowledge").json()
    conflict = next(row for row in bundle["source_snapshots"]
                    if row["snapshot_kind"] == "identity_conflict")
    assert conflict["payload"]["declared_metadata"]["arxiv_id"] == "2301.00001v1"


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
    # Version 3 carries Work/Version/SourceSnapshot alongside the legacy bibliography projection.
    assert data["schema_version"] == 3
    assert data["owner"] == LOCAL_OWNER
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


def test_workspace_bundle_preview_is_read_only_and_import_is_idempotent_owner_local(client, tmp_path):
    from re0.workspace import Workspace, managed_workspace_path

    source = Workspace(tmp_path / "mcp-source").open()
    source_id = source.record({"source_url": "https://export.arxiv.org/abs/2501.12345",
                               "locator": "metadata from arxiv", "kind": "paper",
                               "content": "Fixture source bundle body",
                               "paper": {"title": "Fixture Bundle Paper"}},
                              tool="search_papers")
    bundle = source.bundle()
    target = managed_workspace_path(client.app.state.store.db.path, "local", source.workspace_id)
    assert not target.exists()

    preview = client.post("/api/workspaces/import/preview", json={"bundle": bundle})
    assert preview.status_code == 200, preview.text
    assert preview.json()["new"] == [source_id] and preview.json()["applied"] is False
    assert not target.exists(), "preview must not create a workspace marker or source file"
    assert client.get("/api/workspaces").json()["workspaces"] == []

    imported = client.post("/api/workspaces/import", json={"bundle": bundle})
    assert imported.status_code == 201, imported.text
    assert imported.json()["papers_approved"] == 0
    assert imported.json()["provenance_verified"] is False
    rows = client.get("/api/workspaces").json()["workspaces"]
    assert len(rows) == 1 and rows[0]["workspace_id"] == source.workspace_id
    assert rows[0]["sources"][0]["source_id"] == source_id
    assert rows[0]["sources"][0]["imported_by_user"] is True
    exported = client.get(f"/api/workspaces/{source.workspace_id}/export")
    assert exported.status_code == 200
    assert exported.headers["content-disposition"].endswith(
        f'filename="re0-workspace-{source.workspace_id}.json"')
    assert exported.json()["sources"][0]["imported_by_user"] is True
    repeated = client.post("/api/workspaces/import", json={"bundle": bundle})
    assert repeated.status_code == 201 and repeated.json()["new"] == []
    assert repeated.json()["already_present"] == [source_id]
    assert client.get("/api/papers").json() == [], "a source bundle never imports or approves papers"


def test_workspace_import_reports_conflicting_source_without_storing_it(client, tmp_path):
    from re0.workspace import Workspace

    source = Workspace(tmp_path / "mcp-source").open()
    source.record({"source_url": "https://export.arxiv.org/abs/2501.12345",
                   "locator": "metadata", "kind": "paper", "content": "fixture"},
                  tool="search_papers")
    bundle = source.bundle()
    bundle["sources"][0]["api_key"] = "must be rejected"
    response = client.post("/api/workspaces/import", json={"bundle": bundle})
    assert response.status_code == 201, response.text
    assert response.json()["new"] == []
    assert response.json()["conflicts"][0]["fields"] == ["api_key"]
    assert client.get("/api/workspaces").json()["workspaces"][0]["sources"] == []


def test_fulltext_snapshot_import_is_previewed_version_bound_and_idempotent(client, tmp_path):
    paper_row = paper(client, arxiv_id="2501.12345v1", version_label="v1", notes="keep this note")
    version_rows = client.get(f"/api/papers/{paper_row['id']}/knowledge").json()["versions"]
    version_id = next(row["id"] for row in version_rows if row["arxiv_id"] == "2501.12345v1")

    bundle = fulltext_bundle(tmp_path)
    workspace_import = client.post("/api/workspaces/import", json={"bundle": bundle})
    assert workspace_import.status_code == 201, workspace_import.text
    # The content-addressed identifier is retained when the workspace bundle is imported.
    source_id = workspace_import.json()["new"][0]
    listed_source = client.get("/api/workspaces").json()["workspaces"][0]["sources"][0]
    assert listed_source["fulltext"]["identifier"] == "2501.12345v1"
    assert "content" not in listed_source, "workspace listing is metadata-only"
    request = {"workspace_id": bundle["workspace_id"], "source_ids": [source_id],
               "paper_version_id": version_id}
    base = f"/api/papers/{paper_row['id']}/knowledge/fulltext"

    preview = client.post(base + "/preview", json=request)
    assert preview.status_code == 200, preview.text
    assert preview.json()["applied"] is False and preview.json()["provenance_verified"] is False
    assert preview.json()["new"][0]["content"].startswith("[§2¶1]")
    assert preview.json()["new"][0]["fulltext"]["parser_version"] == "1"
    before = client.get(f"/api/papers/{paper_row['id']}/knowledge").json()
    assert not any(row["snapshot_kind"] == "fulltext_chunk" for row in before["source_snapshots"])
    assert client.get(f"/api/papers/{paper_row['id']}").json()["notes"] == "keep this note"

    applied = client.post(base + "/import", json=request)
    assert applied.status_code == 200, applied.text
    snapshot_id = applied.json()["new"][0]["source_snapshot_id"]
    assert applied.json()["new"][0]["provenance_verified"] is False
    after = client.get(f"/api/papers/{paper_row['id']}/knowledge").json()
    snapshot = next(row for row in after["source_snapshots"] if row["id"] == snapshot_id)
    assert snapshot["snapshot_kind"] == "fulltext_chunk"
    assert snapshot["paper_version_id"] == version_id
    assert snapshot["locator"] == "§2¶1–§2¶3"
    assert snapshot["payload"]["content"].startswith("[§2¶1]")
    assert snapshot["payload"]["provenance_verified"] is False

    repeated = client.post(base + "/import", json=request)
    assert repeated.status_code == 200 and repeated.json()["new"] == []
    assert repeated.json()["already_present"][0]["source_snapshot_id"] == snapshot_id

    claim = {"relation_type": "claim", "target": "method", "statement": "The paper describes a method.",
             "conditions": "Only for the cited version and paragraph.", "assertion_kind": "model_inference",
             "source_snapshot_id": snapshot_id, "paper_version_id": version_id, "locator": "§2¶2"}
    refused = client.post(f"/api/papers/{paper_row['id']}/relations", json=claim)
    assert refused.status_code == 422 and "人工核对" in refused.json()["detail"]
    claim["assertion_kind"] = "human_confirmation"
    accepted = client.post(f"/api/papers/{paper_row['id']}/relations", json=claim)
    assert accepted.status_code == 201, accepted.text
    assert accepted.json()["paper_version_id"] == version_id
    claim["locator"] = "§9¶99"
    outside_chunk = client.post(f"/api/papers/{paper_row['id']}/relations", json=claim)
    assert outside_chunk.status_code == 422 and "实际段落／页码 locator" in outside_chunk.json()["detail"]

    updated = {key: value for key, value in paper_row.items()
               if key not in {"id", "created_at", "updated_at", "is_demo", "resources"}}
    updated.update(arxiv_id="2501.12345v2", version_label="v2")
    assert client.put(f"/api/papers/{paper_row['id']}", json=updated).status_code == 200
    version_rows = client.get(f"/api/papers/{paper_row['id']}/knowledge").json()["versions"]
    version_v2 = next(row["id"] for row in version_rows if row["arxiv_id"] == "2501.12345v2")
    mismatch = client.post(base + "/preview", json={**request, "paper_version_id": version_v2})
    assert mismatch.status_code == 200
    assert mismatch.json()["new"] == []
    assert mismatch.json()["conflicts"][0]["source_id"] == source_id


def test_owner_workspace_bundle_survives_app_restart(tmp_path):
    from re0.workspace import Workspace

    db_path = str(tmp_path / "workspace-persistence.sqlite3")
    source = Workspace(tmp_path / "external").open()
    source_id = source.record({"source_url": "https://export.arxiv.org/abs/2501.12345",
                               "locator": "section 2", "kind": "paper", "content": "persistent fixture"},
                              tool="search_papers")
    with TestClient(create_app(db_path), headers={"X-Re0-Client": "web"}) as first:
        imported = first.post("/api/workspaces/import", json={"bundle": source.bundle()})
        assert imported.status_code == 201, imported.text

    with TestClient(create_app(db_path), headers={"X-Re0-Client": "web"}) as restarted:
        response = restarted.get("/api/workspaces")
        assert response.status_code == 200
        assert response.json()["workspaces"][0]["sources"][0]["source_id"] == source_id
        assert restarted.get(f"/api/workspaces/{source.workspace_id}/export").json()["sources"][0]["content"] == "persistent fixture"
