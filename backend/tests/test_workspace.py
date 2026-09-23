"""Tests for the opt-in workspace.

The default surface is stateless, so every rule here is about what the opt-in mode refuses. A
workspace that accepted anything would turn "a tool returned this" into "we have verified this",
which is the one thing the evidence layer must not do.
"""
import shutil

import pytest

from re0.workspace import Workspace, WorkspaceError, source_id

DOCUMENT = {
    "source_url": "https://arxiv.org/abs/2605.11818",
    "locator": "metadata from openalex",
    "kind": "paper",
    "content": "TEST FIXTURE: a layer study.",
    "paper": {"title": "RevealLayer", "authors": ["A"]},
    "publication": {"state": "preprint", "label": "仅见预印本版本"},
    "artifact_candidates": [{"url": "https://github.com/360CVGroup/RevealLayer", "origin": "name match"}],
    "artifact_search": "searched",
}


def test_a_workspace_stores_a_tool_source_with_a_stable_id(tmp_path):
    workspace = Workspace(tmp_path / "ws").open()
    assert workspace.workspace_id.startswith("ws_")
    first = workspace.record(DOCUMENT, tool="search_papers")
    second = workspace.record(dict(DOCUMENT), tool="search_papers")
    # Content-addressed: the same source recorded twice is the same source, not two.
    assert first == second and first.startswith("src_")
    stored = workspace.read(first)
    assert stored["origin"] == "tool" and stored["tool"] == "search_papers"
    assert stored["artifact_candidates"][0]["url"].endswith("360CVGroup/RevealLayer")
    # The document is bounded on the way in, so a huge body cannot be smuggled through.
    assert len(stored["content"]) <= 256 * 1024


def test_model_text_cannot_be_recorded_as_if_a_tool_returned_it(tmp_path):
    workspace = Workspace(tmp_path / "ws").open()
    with pytest.raises(WorkspaceError) as caught:
        workspace.record({"origin": "model", "content": "the model wrote this"}, tool="search_papers")
    assert "not evidence" in str(caught.value)
    assert workspace.identifiers() == []


def test_a_source_id_is_a_filename_so_anything_else_is_refused(tmp_path):
    workspace = Workspace(tmp_path / "ws").open()
    for bad in ("../escape", "src_../../etc/passwd", "/absolute", "src_zzzz", ""):
        with pytest.raises(WorkspaceError):
            workspace.read(bad)


def test_import_previews_by_default_then_is_idempotent_on_apply(tmp_path):
    origin = Workspace(tmp_path / "one").open()
    origin.record(DOCUMENT, tool="search_papers")
    bundle = origin.bundle()

    target = Workspace(tmp_path / "two")
    # A different directory is a different workspace, so its own bundle is what it can read.
    target.open()
    with pytest.raises(WorkspaceError) as caught:
        target.import_bundle(bundle)
    assert "其他工作区" in str(caught.value)

    # A workspace copied to another directory keeps its identity, marker and all.
    moved = tmp_path / "three"
    moved.mkdir()
    shutil.copy(origin.root / ".re0-workspace.json", moved / ".re0-workspace.json")
    delivered = Workspace(moved).open()
    assert delivered.workspace_id == origin.workspace_id
    preview = delivered.import_bundle(bundle)
    assert preview["applied"] is False and len(preview["new"]) == 1
    # A preview writes nothing, which is the point of previewing.
    assert delivered.identifiers() == []

    applied = delivered.import_bundle(bundle, apply=True)
    assert applied["applied"] is True and len(applied["new"]) == 1
    again = delivered.import_bundle(bundle, apply=True)
    assert again["new"] == [] and len(again["already_present"]) == 1


def test_an_import_never_approves_a_paper_and_says_so(tmp_path):
    origin = Workspace(tmp_path / "one").open()
    origin.record(DOCUMENT, tool="search_papers")
    result = origin.import_bundle(origin.bundle(), apply=True)
    assert result["papers_approved"] == 0
    assert "human-confirmed" in result["note"]


def test_import_to_a_new_directory_previews_without_writing_then_records_unverified_transfer(tmp_path):
    origin = Workspace(tmp_path / "origin").open()
    origin.record(DOCUMENT, tool="search_papers")
    bundle = origin.bundle()
    target = tmp_path / "new destination"

    preview_workspace, preview = Workspace.import_to(target, bundle)
    assert preview_workspace.workspace_id == origin.workspace_id
    assert preview["applied"] is False and len(preview["new"]) == 1
    assert not target.exists(), "preview must not create its destination or marker"

    imported_workspace, applied = Workspace.import_to(target, bundle, apply=True)
    source = applied["new"][0]
    assert imported_workspace.workspace_id == origin.workspace_id
    assert applied["applied"] and applied["papers_approved"] == 0
    assert imported_workspace.read(source)["imported_by_user"] is True
    assert Workspace.import_to(target, bundle)[1]["already_present"] == [source]


def test_bundle_import_refuses_unknown_fields_and_oversized_sources_without_dropping_them(tmp_path):
    origin = Workspace(tmp_path / "origin").open()
    origin.record(DOCUMENT, tool="search_papers")
    bundle = origin.bundle()
    bundle["sources"][0]["api_key"] = "must not be stored or silently discarded"
    target = tmp_path / "target"
    workspace, report = Workspace.import_to(target, bundle, apply=True)
    assert report["new"] == [] and report["conflicts"][0]["fields"] == ["api_key"]
    assert workspace.identifiers() == []
    assert not report["provenance_verified"]

    oversized = origin.bundle()
    oversized["sources"][0]["content"] = "x" * (256 * 1024 + 1)
    _workspace, report = Workspace.import_to(tmp_path / "large", oversized)
    assert report["new"] == [] and "大小上限" in report["conflicts"][0]["reason"]


def test_a_corrupt_marker_is_refused_without_replacing_it(tmp_path):
    root = tmp_path / "broken"
    root.mkdir()
    marker = root / ".re0-workspace.json"
    marker.write_text("not-json", encoding="utf-8")
    with pytest.raises(WorkspaceError):
        Workspace(root).open()
    assert marker.read_text(encoding="utf-8") == "not-json"


def test_a_bundle_that_is_not_a_workspace_bundle_is_refused(tmp_path):
    workspace = Workspace(tmp_path / "ws").open()
    for bad in ({}, {"schema_version": "99", "sources": []}, {"schema_version": "1"},
                {"schema_version": "1", "workspace_id": workspace.workspace_id, "sources": "nope"}):
        with pytest.raises(WorkspaceError):
            workspace.import_bundle(bad)


def test_recording_metadata_is_outside_the_identity(tmp_path):
    """The probe that first exercised this caught four files for two sources: the timestamp was
    inside the id, so recording the same source a second later minted a second id."""
    workspace = Workspace(tmp_path / "ws").open()
    first = workspace.record(DOCUMENT, tool="search_papers")
    # Same source, later, recorded by another tool: one id, and the first record is kept.
    again = workspace.record(dict(DOCUMENT), tool="inspect_resource")
    assert again == first
    assert workspace.identifiers() == [first]
    assert workspace.read(first)["tool"] == "search_papers"
    assert workspace.read(first)["retrieved_at"]


def test_a_source_recorded_without_an_origin_is_treated_as_a_tool_source(tmp_path):
    """The default is what keeps an older caller working, and the stored record still states its
    origin explicitly so a reader never has to infer it."""
    workspace = Workspace(tmp_path / "ws").open()
    identifier = workspace.record({"content": "a source with no origin field"}, tool="search_repositories")
    assert workspace.read(identifier)["origin"] == "tool"
    assert source_id({"a": 1}) == source_id({"a": 1})
