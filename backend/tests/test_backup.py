from pathlib import Path
import importlib.util
import sqlite3

import pytest
from re0.db import restore_database

path = Path(__file__).resolve().parents[2] / "scripts/backup.py"
spec = importlib.util.spec_from_file_location("backup_script", path)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
backup_database = module.backup_database


def test_backup_captures_live_wal_and_is_readable(tmp_path):
    source = tmp_path / "source.sqlite3"
    target = tmp_path / "backup.sqlite3"
    with sqlite3.connect(source) as con:
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("CREATE TABLE notes (body TEXT)")
        con.execute("INSERT INTO notes VALUES (?)", ("研究笔记",))
        con.commit()
        backup_database(source, target)
        with sqlite3.connect(target) as restored:
            assert restored.execute("SELECT body FROM notes").fetchone()[0] == "研究笔记"
    con.close()


def test_missing_source_never_creates_empty_database(tmp_path):
    source, target = tmp_path / "missing.sqlite3", tmp_path / "output.sqlite3"
    with pytest.raises(FileNotFoundError):
        backup_database(source, target)
    assert not source.exists() and not target.exists()


def test_existing_backup_never_overwritten(tmp_path):
    source, target = tmp_path / "source.sqlite3", tmp_path / "output.sqlite3"
    source.touch()
    target.write_text("keep me")
    with pytest.raises(FileExistsError):
        backup_database(source, target)
    assert target.read_text() == "keep me"


def test_invalid_source_cleans_partial_backup(tmp_path):
    source, target = tmp_path / "invalid.sqlite3", tmp_path / "output.sqlite3"
    source.write_text("not a database")
    with pytest.raises(sqlite3.DatabaseError):
        backup_database(source, target)
    assert not target.exists()


def test_restore_requires_a_re0_database_and_cleans_invalid_copy(tmp_path):
    source, target = tmp_path / "unrelated.sqlite3", tmp_path / "restored.sqlite3"
    with sqlite3.connect(source) as con:
        con.execute("CREATE TABLE unrelated (value TEXT)")
        con.execute("INSERT INTO unrelated VALUES ('not Re0')")
    with pytest.raises(sqlite3.DatabaseError, match="缺少 Re0 文献库表"):
        restore_database(source, target)
    assert not target.exists()


def test_restore_never_overwrites_an_existing_destination(tmp_path):
    source, target = tmp_path / "backup.sqlite3", tmp_path / "restored.sqlite3"
    from re0.db import Database
    Database(str(source))
    target.write_text("keep this file", encoding="utf-8")
    with pytest.raises(FileExistsError, match="未覆盖"):
        restore_database(source, target)
    assert target.read_text(encoding="utf-8") == "keep this file"


def test_audit_records_survive_a_backup_and_restore_cycle(tmp_path):
    """Issue #6's data has to come back readable, and it does so without a migration.

    The audit and the human confirmation both live inside the existing `observations.data` blob, so
    this is a test that no schema change was needed — not a claim that one was handled.
    """
    from re0.db import Database
    from re0.models import PaperInput, ResourceAudit, ResourceInput
    from re0.deployment import LOCAL_OWNER
    from re0.service import Store

    source, restored_path = tmp_path / "live.sqlite3", tmp_path / "restored.sqlite3"
    store = Store(Database(str(source)))
    paper = store.create_paper(PaperInput(title="LayerKit: A Study", arxiv_id="2401.00001",
                                          notes="我自己的笔记"), owner=LOCAL_OWNER)
    resource = store.create_resource(paper["id"], ResourceInput(
        kind="code", label="lab/layerkit", url="https://github.com/lab/layerkit"),
        owner=LOCAL_OWNER)
    observation = store.save_observation(resource["id"], {
        "status": "metadata_accessible", "checked_at": "2026-09-22T15:46:49+00:00",
        "resource_audit": {"status": "metadata_readable", "revision": "a" * 40}},
        owner=LOCAL_OWNER)
    confirmation = store.confirm_resource(resource["id"], ResourceAudit.model_validate({
        "paper_title": "LayerKit: A Study", "resource_url": "https://github.com/lab/layerkit",
        "status": "metadata_readable", "attribution": "official",
        "attribution_evidence": [{"source_url": "https://arxiv.org/abs/2401.00001",
                                  "locator": "论文第 3 页", "excerpt": "代码发布于 lab/layerkit。"}]}),
        owner=LOCAL_OWNER)
    assert observation["record_kind"] == "observation"
    assert confirmation["record_kind"] == "confirmation"

    backup_path = tmp_path / "archive.sqlite3"
    backup_database(source, backup_path)
    restore_database(backup_path, restored_path)
    after = Store(Database(str(restored_path)))
    history = after.history(resource["id"], owner=LOCAL_OWNER)
    assert [row["record_kind"] for row in history] == ["confirmation", "observation"]
    assert history[0]["attribution"] == "official"
    assert history[1]["resource_audit"]["revision"] == "a" * 40
    # The reader's notes are what an import or a restore must never cost them.
    assert after.get_paper(paper["id"], owner=LOCAL_OWNER)["notes"] == "我自己的笔记"
