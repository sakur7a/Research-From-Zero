"""Ship the skill directory, and install it somewhere a host can find it.

Two operations, one rule: **nothing is overwritten without saying so.** A host that already has a
skill by this name may have edited it, and quietly replacing that file destroys work with no
record. So an install previews every file first, classifies each as new / identical / conflicting,
refuses the conflicting ones, and `--force` still renames the old file aside rather than deleting
it.

The skill lives at `skills/re0-paper-search/` in a checkout and under the installed data directory
in a wheel. `locate_skill()` reports which one it found and how, because "installed from the wheel"
and "picked up from a checkout next door" are different facts and a host debugging a stale skill
needs to know which it is running.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

SKILL_NAME = "re0-paper-search"
# Never shipped: byte-code, editor droppings and anything a local run left behind.
EXCLUDED_NAMES = frozenset({"__pycache__", ".DS_Store", ".pytest_cache"})
EXCLUDED_SUFFIXES = frozenset({".pyc", ".pyo", ".tmp", ".bak"})
FILE_STATES = ("new", "identical", "conflict")


class SkillPackageError(Exception):
    """A refusal with a reason the caller can act on."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def locate_skill() -> tuple[Path, str]:
    """Find the skill directory, and say how it was found.

    Order matters: an explicit override beats an installed copy, which beats a checkout. A
    checkout is tried last so that a host which installed the distribution runs what it installed
    rather than whatever source tree happens to sit nearby.
    """
    override = os.getenv("RE0_SKILL_DIR", "").strip()
    if override:
        candidate = Path(override).expanduser()
        if (candidate / SKILL_NAME / "SKILL.md").is_file():
            return candidate / SKILL_NAME, f"RE0_SKILL_DIR={override}"
        if (candidate / "SKILL.md").is_file():
            return candidate, f"RE0_SKILL_DIR={override}"
        raise SkillPackageError(
            f"RE0_SKILL_DIR={override} does not contain {SKILL_NAME}/SKILL.md or SKILL.md")
    here = Path(__file__).resolve().parent
    # A wheel/sdist installs the skill under the data directory: <prefix>/share/re0/skills.
    for prefix in {sys.prefix, getattr(sys, "base_prefix", sys.prefix)}:
        candidate = Path(prefix) / "share" / "re0" / "skills" / SKILL_NAME
        if (candidate / "SKILL.md").is_file():
            return candidate, f"installed data directory {candidate.parent.parent.parent}"
    # A checkout: <repo>/backend/re0/skill_package.py -> <repo>/skills/<skill>
    for parent in here.parents:
        candidate = parent / "skills" / SKILL_NAME
        if (candidate / "SKILL.md").is_file():
            return candidate, f"repository checkout {parent}"
    raise SkillPackageError(
        "找不到 skill 目录。它应随分发包安装在 <prefix>/share/re0/skills 下，或在仓库的 skills/ 里；"
        "也可以用 RE0_SKILL_DIR 指定。")


def skill_files(root: Path) -> list[tuple[str, Path]]:
    """Every shippable file under `root` as (relative posix path, absolute path), in a fixed order.

    The order is what makes a manifest reproducible: the same tree hashes to the same document on
    any machine, so two builds can be compared byte for byte instead of by directory-walk order.
    """
    found = []
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        relative = path.relative_to(root)
        if any(part in EXCLUDED_NAMES for part in relative.parts):
            continue
        if path.suffix in EXCLUDED_SUFFIXES:
            continue
        found.append((relative.as_posix(), path))
    # Sorted by the posix string, not by Path: Path comparison is case-insensitive on Windows and
    # case-sensitive elsewhere, so sorting it would make the manifest depend on the build machine.
    found.sort(key=lambda item: item[0])
    if not any(relative == "SKILL.md" for relative, _ in found):
        raise SkillPackageError(f"{root} 里没有 SKILL.md，不是一个可交付的 skill 目录")
    return found


def manifest(root: Path, *, origin: str = "") -> dict:
    """A versioned description of the tree: what is in it, and what each file hashes to.

    Hashes are what make an install checkable afterwards. A host can re-run the manifest over the
    directory it received and see whether anything drifted, without trusting a timestamp.
    """
    from re0.cli import version
    entries = [{"path": relative, "sha256": _digest(path), "bytes": path.stat().st_size}
               for relative, path in skill_files(root)]
    return {"schema_version": "1", "skill": SKILL_NAME, "generated_at": _now(),
            "re0_version": version(), "origin": origin, "files": entries,
            "tree_sha256": hashlib.sha256(
                "".join(f"{entry['path']}:{entry['sha256']}\n" for entry in entries)
                .encode()).hexdigest()}


@dataclass
class FilePlan:
    source: Path
    relative: str
    destination: Path
    state: str
    sha256: str
    detail: str = ""


@dataclass
class InstallPlan:
    target: Path
    skill: Path
    origin: str
    files: list = field(default_factory=list)

    @property
    def new(self) -> list:
        return [item for item in self.files if item.state == "new"]

    @property
    def identical(self) -> list:
        return [item for item in self.files if item.state == "identical"]

    @property
    def conflicts(self) -> list:
        return [item for item in self.files if item.state == "conflict"]

    def as_dict(self) -> dict:
        return {"target": str(self.target), "skill": str(self.skill), "origin": self.origin,
                "counts": {state: sum(1 for item in self.files if item.state == state)
                           for state in FILE_STATES},
                "files": [{"path": item.relative, "state": item.state, "sha256": item.sha256,
                           "detail": item.detail} for item in self.files]}


def plan_install(target: str | Path, *, skill_root: Path | None = None, origin: str = "") -> InstallPlan:
    """Classify every file an install would touch, without touching anything.

    This is the whole point of the preview: the answer to "what would this do to my directory" is
    computed before a single byte is written, so a host can read it and decide.
    """
    if skill_root is None:
        skill_root, origin = locate_skill()
    destination = Path(target).expanduser()
    if not str(destination).strip():
        raise SkillPackageError("需要一个 --target 目录")
    resolved_target = destination.resolve()
    resolved_skill = skill_root.resolve()
    if resolved_target == resolved_skill or resolved_skill in resolved_target.parents:
        raise SkillPackageError(
            f"目标 {destination} 就是 skill 目录本身或它的上级；安装会是把目录复制到它自己里面")
    if resolved_target.exists() and not resolved_target.is_dir():
        raise SkillPackageError(f"{destination} 已存在且不是目录")
    plan = InstallPlan(target=destination, skill=skill_root, origin=origin)
    for relative, source in skill_files(skill_root):
        destination_file = (destination / relative)
        # A relative path from the source tree must not be able to escape the target. `..` and an
        # absolute path are both refused rather than normalised away silently.
        if destination_file.resolve().parent != resolved_target and \
                resolved_target not in destination_file.resolve().parents:
            raise SkillPackageError(f"拒绝写入目标目录之外：{relative}")
        digest = _digest(source)
        if not destination_file.exists():
            state, detail = "new", ""
        elif _digest(destination_file) == digest:
            state, detail = "identical", "内容一致，无需写入"
        else:
            state = "conflict"
            detail = ("目标已有同名但内容不同的文件；不会覆盖。"
                      "加 --force 会把旧文件改名保留为 .re0-backup-<时间戳> 而不是删除")
        plan.files.append(FilePlan(source=source, relative=relative, destination=destination_file,
                                   state=state, sha256=digest, detail=detail))
    return plan


def _backup(path: Path) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    candidate = path.with_name(path.name + f".re0-backup-{stamp}")
    suffix = 1
    while candidate.exists():
        candidate = path.with_name(path.name + f".re0-backup-{stamp}-{suffix}")
        suffix += 1
    path.rename(candidate)
    return candidate


def apply_install(plan: InstallPlan, *, force: bool = False) -> dict:
    """Write the files the plan says are new, and nothing else.

    `identical` files are skipped rather than rewritten, so re-running an install does not churn
    timestamps. `conflict` files are refused unless `force`, and even then the previous content is
    renamed aside: a host's edits are recoverable, which overwriting would not be.
    """
    written, skipped, backed_up, refused = [], [], [], []
    for item in plan.files:
        if item.state == "identical":
            skipped.append(item.relative)
            continue
        if item.state == "conflict" and not force:
            refused.append(item.relative)
            continue
        item.destination.parent.mkdir(parents=True, exist_ok=True)
        if item.state == "conflict":
            backed_up.append({"path": item.relative,
                              "backup": _backup(item.destination).name})
        shutil.copyfile(item.source, item.destination)
        written.append(item.relative)
    manifest_path = plan.target / "MANIFEST.json"
    manifest_path.write_text(json.dumps(manifest(plan.skill, origin=plan.origin),
                                        ensure_ascii=False, indent=2), encoding="utf-8")
    return {"target": str(plan.target), "written": written, "skipped_identical": skipped,
            "backed_up": backed_up, "refused": refused, "manifest": str(manifest_path),
            "blocked": bool(refused)}


def render_plan(plan: InstallPlan, *, applied: dict | None = None) -> str:
    """Human-readable preview. Printed before anything is written, and again after."""
    lines = [f"skill: {plan.skill}", f"来源: {plan.origin or '未记录'}",
             f"目标: {plan.target}",
             f"文件: 新增 {len(plan.new)} · 内容一致 {len(plan.identical)} · 冲突 {len(plan.conflicts)}"]
    for item in plan.files:
        marker = {"new": "+", "identical": "=", "conflict": "!"}[item.state]
        lines.append(f"  {marker} {item.relative}  [{item.state}]  {item.sha256[:12]}")
        if item.detail:
            lines.append(f"      {item.detail}")
    if plan.conflicts:
        lines.append("有冲突文件；本次不会覆盖它们。确认要替换请加 --force（旧文件会改名保留）。")
    if applied is not None:
        lines.append(f"已写入 {len(applied['written'])} 个文件；跳过内容一致 {len(applied['skipped_identical'])} 个")
        for entry in applied["backed_up"]:
            lines.append(f"  旧文件保留为 {entry['backup']}（原 {entry['path']}）")
        if applied["refused"]:
            lines.append(f"未写入（冲突且未加 --force）：{'、'.join(applied['refused'])}")
        lines.append(f"清单: {applied['manifest']}")
    return "\n".join(lines)


def package_into(output: str | Path, *, skill_root: Path | None = None, origin: str = "") -> dict:
    """Write a self-contained, hash-manifested copy of the skill for hand-delivery.

    A copy rather than an archive: a host can read it, diff it and install from it without an
    unpacking step, and the manifest travels with it so the copy can be checked later.
    """
    if skill_root is None:
        skill_root, origin = locate_skill()
    destination = Path(output).expanduser() / SKILL_NAME
    if destination.exists() and any(destination.iterdir()):
        raise SkillPackageError(f"{destination} 已存在且非空；换一个 --output，或先移走它。不会覆盖")
    plan = plan_install(destination, skill_root=skill_root, origin=origin)
    applied = apply_install(plan)
    return {**applied, "packaged_from": str(skill_root), "origin": origin}
