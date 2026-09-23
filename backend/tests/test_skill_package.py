"""Skill packaging: locate, manifest, preview, and an install that never overwrites silently."""
import json
import tomllib
from pathlib import Path

import pytest

from re0 import skill_package
from re0.cli import main
from re0.skill_package import (SKILL_NAME, SkillPackageError, apply_install, locate_skill,
                               manifest, package_into, plan_install, render_plan, skill_files)

REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def fake_skill(tmp_path):
    """A stand-in skill tree, so the tests do not depend on the real one's contents."""
    root = tmp_path / "src" / SKILL_NAME
    (root / "scripts").mkdir(parents=True)
    (root / "SKILL.md").write_text("---\nname: fake\n---\n\n# Fake skill\n", encoding="utf-8")
    (root / "scripts" / "paper_search.py").write_text("raise SystemExit(0)\n", encoding="utf-8")
    (root / "scripts" / "__pycache__").mkdir()
    (root / "scripts" / "__pycache__" / "paper_search.cpython-312.pyc").write_bytes(b"\x00\x01")
    (root / ".DS_Store").write_bytes(b"junk")
    return root


# --- locating -----------------------------------------------------------------


def test_the_checkout_is_found_and_the_origin_is_named(monkeypatch):
    monkeypatch.delenv("RE0_SKILL_DIR", raising=False)
    root, origin = locate_skill()
    assert (root / "SKILL.md").is_file() and root.name == SKILL_NAME
    assert "repository checkout" in origin or "installed data directory" in origin


def test_an_explicit_override_wins_and_a_bad_one_fails_loudly(tmp_path, fake_skill, monkeypatch):
    monkeypatch.setenv("RE0_SKILL_DIR", str(fake_skill.parent))
    root, origin = locate_skill()
    assert root == fake_skill and "RE0_SKILL_DIR" in origin
    monkeypatch.setenv("RE0_SKILL_DIR", str(fake_skill))
    assert locate_skill()[0] == fake_skill, "pointing straight at the skill also works"
    monkeypatch.setenv("RE0_SKILL_DIR", str(tmp_path / "nowhere"))
    with pytest.raises(SkillPackageError) as caught:
        locate_skill()
    assert "does not contain" in str(caught.value)


# --- what gets shipped --------------------------------------------------------


def test_byte_code_and_editor_droppings_are_not_shipped(fake_skill):
    relative = [name for name, _ in skill_files(fake_skill)]
    assert relative == ["SKILL.md", "scripts/paper_search.py"]
    assert not any("__pycache__" in name or name.endswith(".pyc") for name in relative)


def test_a_directory_without_a_skill_file_is_not_a_skill(tmp_path):
    empty = tmp_path / "not-a-skill"
    empty.mkdir()
    (empty / "readme.txt").write_text("nothing here", encoding="utf-8")
    with pytest.raises(SkillPackageError) as caught:
        skill_files(empty)
    assert "SKILL.md" in str(caught.value)


def test_the_manifest_is_reproducible_and_moves_when_a_file_does(fake_skill):
    first = manifest(fake_skill, origin="test")
    again = manifest(fake_skill, origin="test")
    assert first["tree_sha256"] == again["tree_sha256"], "same tree, same hash, regardless of walk order"
    assert [entry["path"] for entry in first["files"]] == ["SKILL.md", "scripts/paper_search.py"]
    assert all(len(entry["sha256"]) == 64 for entry in first["files"])
    (fake_skill / "SKILL.md").write_text("changed\n", encoding="utf-8")
    assert manifest(fake_skill)["tree_sha256"] != first["tree_sha256"]


def test_the_pyproject_data_files_list_matches_the_real_tree():
    """Drift guard: TOML cannot glob, so a file added to skills/ and not listed here would simply
    be missing from every wheel built afterwards — silently."""
    document = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    # Only the skill's own groups: `web/` ships through the same mechanism and is pinned by
    # test_paths.py, so comparing every data-files group against `skills/` would fail on any second
    # tree the distribution ever carries.
    declared = {Path(name).name
                for group, names in (document["tool"]["setuptools"]["data-files"] or {}).items()
                if "skills" in group for name in names}
    on_disk = {path.name for path in (REPO_ROOT / "skills" / SKILL_NAME).rglob("*")
               if path.is_file() and "__pycache__" not in path.parts
               and path.suffix not in {".pyc", ".bak", ".tmp"}}
    assert declared == on_disk, (
        "skills/ and pyproject data-files disagree; a wheel would ship an incomplete skill. "
        f"only on disk: {sorted(on_disk - declared)}; only declared: {sorted(declared - on_disk)}")


# --- preview ------------------------------------------------------------------


def test_the_preview_classifies_new_identical_and_conflicting_files(tmp_path, fake_skill):
    target = tmp_path / "host-skills"
    (target / SKILL_NAME / "scripts").mkdir(parents=True)
    (target / SKILL_NAME / "SKILL.md").write_text("a host edited this\n", encoding="utf-8")
    (target / SKILL_NAME / "scripts" / "paper_search.py").write_text(
        (fake_skill / "scripts" / "paper_search.py").read_text(encoding="utf-8"), encoding="utf-8")

    plan = plan_install(target / SKILL_NAME, skill_root=fake_skill)
    states = {item.relative: item.state for item in plan.files}
    assert states == {"SKILL.md": "conflict", "scripts/paper_search.py": "identical"}
    assert len(plan.new) == 0 and len(plan.conflicts) == 1 and len(plan.identical) == 1
    assert "不会覆盖" in plan.conflicts[0].detail
    assert plan.as_dict()["counts"] == {"new": 0, "identical": 1, "conflict": 1}


def test_the_preview_reads_as_a_preview(tmp_path, fake_skill):
    plan = plan_install(tmp_path / "host" / SKILL_NAME, skill_root=fake_skill)
    text = render_plan(plan)
    assert "新增 2" in text and "+ SKILL.md" in text and "+ scripts/paper_search.py" in text
    assert "dry run" not in text, "the preview itself makes no claim about writing"


# --- install ------------------------------------------------------------------


def test_a_dry_run_writes_nothing_at_all(tmp_path, fake_skill, monkeypatch):
    monkeypatch.setenv("RE0_SKILL_DIR", str(fake_skill))
    host = tmp_path / "host-skills"
    assert main(["skill", "install", "--target", str(host), "--dry-run"]) == 0
    assert not host.exists(), "a preview must not even create the directory"


def test_the_target_is_the_host_skills_directory_and_the_skill_lands_in_its_own(tmp_path, fake_skill,
                                                                                monkeypatch):
    """A host keeps one directory per skill, so `--target` names the parent."""
    monkeypatch.setenv("RE0_SKILL_DIR", str(fake_skill))
    host = tmp_path / "host-skills"
    assert main(["skill", "install", "--target", str(host)]) == 0
    assert (host / SKILL_NAME / "SKILL.md").is_file()
    assert (host / SKILL_NAME / "scripts" / "paper_search.py").is_file()
    assert not (host / "SKILL.md").exists(), "nothing is installed loose into the skills root"


def test_installing_writes_the_files_and_a_manifest(tmp_path, fake_skill):
    target = tmp_path / "host" / SKILL_NAME
    plan = plan_install(target, skill_root=fake_skill)
    applied = apply_install(plan)
    assert applied["written"] == ["SKILL.md", "scripts/paper_search.py"]
    assert applied["refused"] == [] and applied["blocked"] is False
    assert (target / "SKILL.md").read_text(encoding="utf-8").startswith("---\nname: fake")
    document = json.loads((target / "MANIFEST.json").read_text(encoding="utf-8"))
    assert document["skill"] == SKILL_NAME and document["schema_version"] == "1"
    assert {entry["path"] for entry in document["files"]} == {"SKILL.md", "scripts/paper_search.py"}
    # The manifest describes what was shipped, so its hashes match the files that arrived.
    for entry in document["files"]:
        assert entry["sha256"] == skill_package._digest(target / entry["path"])


def test_installing_twice_is_idempotent(tmp_path, fake_skill):
    target = tmp_path / "host" / SKILL_NAME
    apply_install(plan_install(target, skill_root=fake_skill))
    second = apply_install(plan_install(target, skill_root=fake_skill))
    assert second["written"] == [] and len(second["skipped_identical"]) == 2
    assert second["blocked"] is False


def test_a_conflicting_file_is_refused_and_left_byte_for_byte_alone(tmp_path, fake_skill):
    target = tmp_path / "host" / SKILL_NAME
    target.mkdir(parents=True)
    (target / "SKILL.md").write_text("a host edited this\n", encoding="utf-8")
    plan = plan_install(target, skill_root=fake_skill)
    applied = apply_install(plan)
    assert applied["refused"] == ["SKILL.md"] and applied["blocked"] is True
    assert applied["written"] == ["scripts/paper_search.py"], "the non-conflicting file still lands"
    assert (target / "SKILL.md").read_text(encoding="utf-8") == "a host edited this\n"
    assert not list(target.glob("*.re0-backup-*")), "refusing must not touch the old file at all"


def test_force_replaces_a_conflict_by_renaming_the_old_file_aside(tmp_path, fake_skill):
    target = tmp_path / "host" / SKILL_NAME
    target.mkdir(parents=True)
    (target / "SKILL.md").write_text("a host edited this\n", encoding="utf-8")
    applied = apply_install(plan_install(target, skill_root=fake_skill), force=True)
    assert applied["written"] == ["SKILL.md", "scripts/paper_search.py"]
    (backup,) = applied["backed_up"]
    assert backup["path"] == "SKILL.md" and backup["backup"].startswith("SKILL.md.re0-backup-")
    assert (target / backup["backup"]).read_text(encoding="utf-8") == "a host edited this\n"
    assert (target / "SKILL.md").read_text(encoding="utf-8").startswith("---\nname: fake")


def test_the_cli_refuses_a_conflict_with_a_distinct_exit_code(tmp_path, fake_skill, monkeypatch, capsys):
    monkeypatch.setenv("RE0_SKILL_DIR", str(fake_skill))
    target = tmp_path / "host" / SKILL_NAME
    target.mkdir(parents=True)
    (target / "SKILL.md").write_text("a host edited this\n", encoding="utf-8")
    assert main(["skill", "install", "--target", str(tmp_path / "host")]) == 3
    printed = capsys.readouterr()
    assert "--force" in printed.err and "renamed aside" in printed.err
    assert (target / "SKILL.md").read_text(encoding="utf-8") == "a host edited this\n"


# --- refusals -----------------------------------------------------------------


def test_installing_onto_the_skill_itself_or_a_parent_of_it_is_refused(fake_skill):
    with pytest.raises(SkillPackageError) as caught:
        plan_install(fake_skill, skill_root=fake_skill)
    assert "复制到它自己里面" in str(caught.value)
    # A target *inside* the skill is refused too: it would write the skill into itself.
    with pytest.raises(SkillPackageError):
        plan_install(fake_skill / "nested", skill_root=fake_skill)


def test_a_target_that_is_an_existing_file_is_refused(tmp_path, fake_skill):
    a_file = tmp_path / "occupied"
    a_file.write_text("not a directory", encoding="utf-8")
    with pytest.raises(SkillPackageError) as caught:
        plan_install(a_file, skill_root=fake_skill)
    assert "不是目录" in str(caught.value)


def test_an_empty_target_is_required_for_packaging(tmp_path, fake_skill, monkeypatch):
    monkeypatch.setenv("RE0_SKILL_DIR", str(fake_skill))
    output = tmp_path / "dist"
    result = package_into(output)
    assert Path(result["target"]) == output / SKILL_NAME
    assert (output / SKILL_NAME / "SKILL.md").is_file()
    assert (output / SKILL_NAME / "MANIFEST.json").is_file()
    with pytest.raises(SkillPackageError) as caught:
        package_into(output)
    assert "不会覆盖" in str(caught.value)


def test_skill_show_reports_the_directory_the_hashes_came_from(monkeypatch, capsys):
    monkeypatch.delenv("RE0_SKILL_DIR", raising=False)
    assert main(["skill", "show"]) == 0
    printed = capsys.readouterr().out
    assert "found via:" in printed and "tree sha256:" in printed and "SKILL.md" in printed


def test_skill_without_an_action_says_what_the_actions_are(capsys):
    assert main(["skill"]) == 2
    assert "install --target" in capsys.readouterr().err
