import json
from pathlib import Path

from re0.workspace import Workspace
from re0.workspace_cli import main


def test_cli_exports_previews_and_imports_a_bundle_between_directories_with_spaces(tmp_path, capsys):
    source = Workspace(tmp_path / "source workspace").open()
    source_id = source.record({"source_url": "https://export.arxiv.org/abs/2501.12345",
                               "locator": "section 2", "kind": "paper", "content": "fixture",
                               "paper": {"title": "Fixture bundle source"}},
                              tool="search_papers")
    output = tmp_path / "chosen output" / "source bundle.json"
    assert main(["export", "--directory", str(source.root), "--output", str(output)]) == 0
    exported = json.loads(capsys.readouterr().out)
    assert exported["sources"] == 1 and exported["workspace_id"] == source.workspace_id
    bundle = json.loads(output.read_text(encoding="utf-8"))
    assert bundle["sources"][0]["content"] == "fixture"

    target = tmp_path / "target workspace"
    assert main(["import", "--directory", str(target), "--input", str(output)]) == 0
    preview = json.loads(capsys.readouterr().out)
    assert preview["applied"] is False and preview["new"] == [source_id]
    assert not target.exists()

    assert main(["import", "--directory", str(target), "--input", str(output), "--apply"]) == 0
    applied = json.loads(capsys.readouterr().out)
    assert applied["applied"] and applied["papers_approved"] == 0
    imported = Workspace(target).open()
    assert imported.workspace_id == source.workspace_id
    assert imported.read(source_id)["imported_by_user"] is True


def test_export_does_not_overwrite_without_force_and_keeps_a_backup(tmp_path, capsys):
    workspace = Workspace(tmp_path / "workspace").open()
    output = tmp_path / "bundle.json"
    output.write_text("owner data", encoding="utf-8")
    assert main(["export", "--directory", str(workspace.root), "--output", str(output)]) == 2
    assert output.read_text(encoding="utf-8") == "owner data"
    capsys.readouterr()
    assert main(["export", "--directory", str(workspace.root), "--output", str(output), "--force"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["backup"] and Path(result["backup"]).read_text(encoding="utf-8") == "owner data"


def test_import_rejects_duplicate_json_keys_and_oversized_files(tmp_path, capsys):
    duplicate = tmp_path / "duplicate.json"
    duplicate.write_text('{"schema_version":"1","schema_version":"1"}', encoding="utf-8")
    assert main(["import", "--directory", str(tmp_path / "target"), "--input", str(duplicate)]) == 2
    assert "duplicate JSON key" in capsys.readouterr().err

    oversized = tmp_path / "too-large.json"
    oversized.write_bytes(b" " * (4 * 1024 * 1024 + 1))
    assert main(["import", "--directory", str(tmp_path / "target"), "--input", str(oversized)]) == 2
    assert "MiB 上限" in capsys.readouterr().err
    assert not (tmp_path / "target").exists()
