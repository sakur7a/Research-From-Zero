from pathlib import Path
import importlib.util
import sqlite3

import pytest

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
