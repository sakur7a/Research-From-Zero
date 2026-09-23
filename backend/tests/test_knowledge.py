"""Issue #10: Work/versions stay separate, and evidence remains bound to what was read."""
import json

from re0.db import Database
from re0.deployment import LOCAL_OWNER
from re0.models import PaperInput, ResourceInput
from re0.service import Store


def test_fulltext_versions_bind_by_archive_identity_not_a_shared_title():
    from re0.knowledge import fulltext_matches_version

    arxiv_v1 = {"source": "arxiv", "identifier": "2501.12345v1", "version": "v1"}
    target_v1 = {"arxiv_id": "2501.12345v1", "version_label": "v1",
                 "metadata": {"arxiv_id": "2501.12345v1", "title": "Same title"}}
    target_v2 = {"arxiv_id": "2501.12345v2", "version_label": "v2",
                 "metadata": {"arxiv_id": "2501.12345v2", "title": "Same title"}}
    assert fulltext_matches_version(arxiv_v1, target_v1)
    assert not fulltext_matches_version(arxiv_v1, target_v2)

    acl = {"source": "acl", "identifier": "2024.acl-long.1"}
    acl_version = {"arxiv_id": "", "version_label": "", "metadata": {
        "paper_url": "https://aclanthology.org/2024.acl-long.1/", "title": "Same title"}}
    assert fulltext_matches_version(acl, acl_version)
    assert not fulltext_matches_version({**acl, "identifier": "2024.findings-acl.1"}, acl_version)


def test_work_versions_and_observations_keep_explicit_arxiv_versions(tmp_path):
    store = Store(Database(str(tmp_path / "knowledge.sqlite3")))
    first = store.create_paper(PaperInput(
        title="Layer study", doi="10.1234/layers", arxiv_id="2401.12345v1",
        version_label="arXiv v1", topics=["图层分解 / 生成", "Layout 生成", "Private bucket"],
        notes="Private notes stay on the Work, outside source snapshots."), owner=LOCAL_OWNER)
    resource = store.create_resource(first["id"], ResourceInput(
        kind="code", label="Repository", url="https://github.com/example/layers"), owner=LOCAL_OWNER)
    observation = store.save_observation(resource["id"], {
        "status": "metadata_readable", "summary": "fixture observation",
        "provider": "github", "checked_at": "2026-01-01T00:00:00+00:00",
        "paper_version_snapshot": {"doi": "10.1234/layers", "arxiv_id": "2401.12345v1",
                                   "version_label": "arXiv v1"},
        "evidence": [{"source_url": "https://github.com/example/layers/tree/v1",
                      "locator": "commit v1", "excerpt": "fixture"}],
    }, owner=LOCAL_OWNER)

    second = store.update_paper(first["id"], PaperInput(
        title="Layer study", doi="10.1234/layers", arxiv_id="2401.12345v2",
        version_label="arXiv v2", topics=["图层分解 / 生成", "Layout 生成", "Private bucket"],
        notes="Private notes stay on the Work, outside source snapshots."), owner=LOCAL_OWNER)
    assert second["id"] == first["id"]

    with store.db.connect() as con:
        work = con.execute("SELECT id,owner FROM works WHERE id=?", (first["id"],)).fetchone()
        assert tuple(work) == (first["id"], LOCAL_OWNER)
        versions = {row["arxiv_id"]: row for row in con.execute(
            "SELECT * FROM paper_versions WHERE work_id=?", (first["id"],))}
        assert set(versions) == {"2401.12345v1", "2401.12345v2"}
        assert versions["2401.12345v1"]["is_current"] == 0
        assert versions["2401.12345v2"]["is_current"] == 1
        assert "Private notes" not in versions["2401.12345v1"]["metadata"]
        source = con.execute("SELECT s.paper_version_id,v.arxiv_id FROM source_snapshots s "
                             "JOIN paper_versions v ON v.id=s.paper_version_id "
                             "WHERE s.observation_id=?", (observation["id"],)).fetchone()
        assert tuple(source) == (versions["2401.12345v1"]["id"], "2401.12345v1")
        rows = list(con.execute("SELECT template_version_id,concept_key,label "
                                "FROM paper_topic_assignments WHERE paper_id=? ORDER BY label",
                                (first["id"],)))
        memberships = {(row["label"], row["concept_key"]) for row in rows}
        assert ("图层分解 / 生成", "layout_and_layers") in memberships
        assert ("Layout 生成", "layout_generation") in memberships
        assert ("Private bucket", None) in memberships
        template = con.execute("SELECT definition FROM topic_template_versions "
                               "WHERE id='layout-layer-template-v1'").fetchone()
        definition = json.loads(template["definition"])
        assert {item["key"] for item in definition["dimensions"]} >= {
            "direction", "task", "method", "input", "output", "experiment_condition"}


def test_same_title_with_distinct_identifiers_creates_distinct_works(tmp_path):
    store = Store(Database(str(tmp_path / "identities.sqlite3")))
    first = store.create_paper(PaperInput(title="Same title", doi="10.1234/one"), owner=LOCAL_OWNER)
    second = store.create_paper(PaperInput(title="Same title", doi="10.1234/two"), owner=LOCAL_OWNER)
    assert first["id"] != second["id"]
    with store.db.connect() as con:
        rows = list(con.execute("SELECT id FROM works WHERE owner=?", (LOCAL_OWNER,)))
    assert {row["id"] for row in rows} == {first["id"], second["id"]}


def test_source_snapshots_from_unversioned_old_observations_stay_unbound(tmp_path):
    store = Store(Database(str(tmp_path / "unknown-version.sqlite3")))
    paper = store.create_paper(PaperInput(title="No version identity"), owner=LOCAL_OWNER)
    resource = store.create_resource(paper["id"], ResourceInput(
        kind="code", label="Repository", url="https://github.com/example/unversioned"),
        owner=LOCAL_OWNER)
    observation = store.save_observation(resource["id"], {
        "status": "access_failed", "summary": "timeout", "provider": "github",
        "checked_at": "2026-01-01T00:00:00+00:00", "evidence": []}, owner=LOCAL_OWNER)
    with store.db.connect() as con:
        snapshot = con.execute("SELECT paper_version_id FROM source_snapshots "
                               "WHERE observation_id=?", (observation["id"],)).fetchone()
        assert snapshot["paper_version_id"] is None


def test_template_versions_topic_membership_relations_and_export(client):
    paper = client.post("/api/papers", json={"title": "Versioned study",
                                              "arxiv_id": "2401.12345v1"}).json()
    resource = client.post(f"/api/papers/{paper['id']}/resources", json={
        "kind": "code", "label": "Repository", "url": "https://github.com/example/study"}).json()
    client.app.state.store.save_observation(resource["id"], {
        "status": "metadata_readable", "summary": "fixture observation",
        "provider": "fixture", "checked_at": "2026-01-01T00:00:00+00:00",
        "paper_version_snapshot": {"doi": "", "arxiv_id": "2401.12345v1", "version_label": ""},
        "evidence": [{"source_url": resource["url"], "locator": "fixture", "excerpt": "TEST FIXTURE"}],
    }, owner=LOCAL_OWNER)
    bundle = client.get(f"/api/papers/{paper['id']}/knowledge").json()
    assert bundle["schema_version"] == 1
    assert bundle["work"]["id"] == paper["id"]
    (version,) = bundle["versions"]
    snapshot = next(item for item in bundle["source_snapshots"]
                    if item["snapshot_kind"] == "resource_observation")
    assert version["arxiv_id"] == "2401.12345v1" and version["is_current"] == 1
    assert snapshot["paper_version_id"] == version["id"]
    assert snapshot["source_url"] == resource["url"]

    templates = client.get("/api/knowledge/templates").json()
    system_template = next(row for row in templates if row["template_key"] == "layout-layer")
    definition = dict(system_template["definition"])
    definition["concepts"] = [*definition["concepts"], {
        "key": "program_synthesis", "kind": "method", "label": "程序合成",
        "aliases": ["program synthesis"], "parent": "layout_and_layers"}]
    revised = client.post("/api/knowledge/templates/layout-layer", json=definition)
    assert revised.status_code == 201, revised.text
    assert revised.json()["version"] == 2 and revised.json()["editable"] is True
    templates_after = client.get("/api/knowledge/templates").json()
    original = next(row for row in templates_after if row["id"] == system_template["id"])
    assert not any(item["key"] == "program_synthesis" for item in original["definition"]["concepts"])

    assigned = client.post(f"/api/papers/{paper['id']}/topic-assignments", json={
        "template_version_id": revised.json()["id"], "concept_key": "program_synthesis"})
    assert assigned.status_code == 201, assigned.text
    assert assigned.json()["concept_key"] == "program_synthesis"
    assert "程序合成" in client.get("/api/papers").json()[0]["topics"]

    relation = client.post(f"/api/papers/{paper['id']}/relations", json={
        "relation_type": "uses_method", "target": "程序合成",
        "statement": "fixture relation, not a real paper claim",
        "assertion_kind": "human_confirmation", "source_snapshot_id": snapshot["id"],
        "paper_version_id": version["id"], "locator": "fixture review"})
    assert relation.status_code == 201, relation.text
    assert relation.json()["source_url"] == resource["url"]
    claim = client.post(f"/api/papers/{paper['id']}/relations", json={
        "relation_type": "claim", "target": "theoretical claim",
        "statement": "fixture claim is intentionally refused without full text",
        "conditions": "fixture condition", "assertion_kind": "model_inference",
        "source_snapshot_id": snapshot["id"], "paper_version_id": version["id"],
        "locator": "abstract"})
    assert claim.status_code == 422 and "全文" in claim.json()["detail"]
    index = client.get("/api/knowledge/relations", params={"relation_type": "uses_method",
                                                          "q": "fixture relation"}).json()
    assert index["total"] == 1 and index["relations"][0]["id"] == relation.json()["id"]
    assert client.get("/api/knowledge/relations", params={"relation_type": "unknown"}).status_code == 422
    exported = client.get("/api/export").json()
    assert exported["schema_version"] == 3
    assert exported["knowledge"][0]["relations"][0]["id"] == relation.json()["id"]


def test_relation_requires_a_located_owned_source_and_conditions_for_claim(client):
    paper = client.post("/api/papers", json={"title": "Relation boundary",
                                              "doi": "10.1234/relation"}).json()
    invalid_claim = client.post(f"/api/papers/{paper['id']}/relations", json={
        "relation_type": "claim", "target": "method", "statement": "claim",
        "assertion_kind": "model_inference", "source_snapshot_id": "missing",
        "locator": "section 3"})
    assert invalid_claim.status_code == 422

    other = client.post("/api/papers", json={"title": "Other work",
                                               "doi": "10.1234/other"}).json()
    own_source = client.get(f"/api/papers/{paper['id']}/knowledge").json()["source_snapshots"][0]
    other_source = client.get(f"/api/papers/{other['id']}/knowledge").json()["source_snapshots"][0]
    # A paper metadata snapshot without a URL is not evidence, even though it exists.
    assert own_source["source_url"] == ""
    unlocated = client.post(f"/api/papers/{paper['id']}/relations", json={
        "relation_type": "claim", "target": "method", "statement": "claim",
        "conditions": "fixture only", "assertion_kind": "model_inference",
        "source_snapshot_id": own_source["id"], "locator": "section 3"})
    assert unlocated.status_code == 422
    cross_work = client.post(f"/api/papers/{paper['id']}/relations", json={
        "relation_type": "claim", "target": "method", "statement": "claim",
        "conditions": "fixture only", "assertion_kind": "model_inference",
        "source_snapshot_id": other_source["id"], "locator": "section 3"})
    assert cross_work.status_code == 404


def test_relation_cannot_attach_an_observation_to_a_different_paper_version(client):
    paper = client.post("/api/papers", json={"title": "Version pair",
                                              "arxiv_id": "2501.12345v1"}).json()
    resource = client.post(f"/api/papers/{paper['id']}/resources", json={
        "kind": "code", "label": "Repository", "url": "https://github.com/example/versions"}).json()
    client.app.state.store.save_observation(resource["id"], {
        "status": "metadata_readable", "summary": "v1 fixture", "provider": "fixture",
        "checked_at": "2026-01-01T00:00:00+00:00",
        "paper_version_snapshot": {"doi": "", "arxiv_id": "2501.12345v1", "version_label": ""},
        "evidence": [{"source_url": resource["url"], "locator": "v1", "excerpt": "fixture"}],
    }, owner=LOCAL_OWNER)
    updated = client.put(f"/api/papers/{paper['id']}", json={
        "title": "Version pair", "arxiv_id": "2501.12345v2", "version_label": "v2"})
    assert updated.status_code == 200
    bundle = client.get(f"/api/papers/{paper['id']}/knowledge").json()
    v1 = next(item for item in bundle["versions"] if item["arxiv_id"] == "2501.12345v1")
    v2 = next(item for item in bundle["versions"] if item["arxiv_id"] == "2501.12345v2")
    source = next(item for item in bundle["source_snapshots"]
                  if item["snapshot_kind"] == "resource_observation")
    refused = client.post(f"/api/papers/{paper['id']}/relations", json={
        "relation_type": "uses_method", "target": "diffusion", "statement": "fixture",
        "assertion_kind": "human_confirmation", "source_snapshot_id": source["id"],
        "paper_version_id": v2["id"], "locator": "v1 source"})
    assert v1["id"] == source["paper_version_id"]
    assert refused.status_code == 422 and "版本" in refused.json()["detail"]
