"""An explicit, opt-in workspace for material a tool actually retrieved.

The default MCP surface is stateless and opens no database, and that stays true. This module only
runs when a caller names a directory, and what it stores is deliberately narrow: snapshots of
sources a **trusted tool** returned, each with a stable content-addressed id.

Three boundaries are enforced here rather than trusted to callers:

* **A tool result is not a claim, and model text is not evidence.** Only payloads marked as coming
  from a tool are recorded; anything a host model wrote is refused, so a workspace cannot end up
  looking like verified evidence it never had.
* **Ids do not travel between workspaces.** Source ids are content-addressed, so the same source
  has the same id everywhere — which is exactly why reading one back is scoped to the workspace that
  recorded it, and why a bundle from another workspace is refused instead of merged.
* **Nothing here approves a paper.** Importing a bundle adds sources; adding a paper to the library
  stays a separate, human-confirmed action.

Import is idempotent, and it previews before it writes unless `apply=True` is passed.
"""
from __future__ import annotations

import hashlib
import json
import pathlib
import re
import time
import uuid

SCHEMA_VERSION = "1"
ID_PATTERN = re.compile(r"^src_[0-9a-f]{16}$")
WORKSPACE_ID_PATTERN = re.compile(r"^ws_[0-9a-f]{16}$")
WORKSPACE_MARKER = ".re0-workspace.json"
MAX_SOURCE_BYTES = 256 * 1024
# An audit row carries the sources its conclusions rest on, so a document can legitimately arrive
# with more beside it than its body alone. The body bound is unchanged; this is room for the check.
MAX_AUDIT_BYTES = 64 * 1024
MAX_BUNDLE_SOURCES = 2000
MAX_BUNDLE_BYTES = 4 * 1024 * 1024
# A source is material a tool returned. Anything else is refused rather than recorded.
TRUSTED_ORIGINS = frozenset({"tool"})


class WorkspaceError(RuntimeError):
    """A refusal a caller can act on. Never a traceback, never a partial write."""


def _canonical(payload: dict) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


# What makes a source that source. Recording metadata — when it was stored and by which tool — is
# deliberately outside the identity: were it inside, recording the same source a minute later would
# mint a second id and the workspace would quietly fill with near-duplicates.
IDENTITY_FIELDS = ("source_url", "locator", "kind", "content", "paper", "publication",
                   "preprint_also", "institutions", "artifact_candidates", "artifact_search")
# Carried beside the identity, never inside it. A resource audit is a check performed at a time, so
# hashing it would make the same repository a different source every time it was re-checked.
RECORDED_FIELDS = IDENTITY_FIELDS + ("artifact_search_detail", "artifact_outcome", "resource_audits",
                                    "fulltext")
# Truncated on the way in; everything else recorded is copied as it arrived.
BOUNDED_FIELDS = ("source_url", "locator", "kind", "content")
COPIED_FIELDS = tuple(name for name in RECORDED_FIELDS if name not in BOUNDED_FIELDS)
WORKSPACE_FIELDS = frozenset({"schema_version", "origin", "tool", "retrieved_at",
                              *RECORDED_FIELDS, "imported_by_user"})


def _identity(payload: dict) -> dict:
    return {key: payload[key] for key in IDENTITY_FIELDS if payload.get(key) is not None}


def source_id(payload: dict) -> str:
    """Content-addressed over the identity fields, so the same source always has the same id."""
    return "src_" + hashlib.sha256(_canonical(_identity(payload)).encode("utf-8")).hexdigest()[:16]


def managed_workspaces_root(database_path, owner: str) -> pathlib.Path:
    """Return a server-selected, owner-separated directory; callers never supply a filesystem path."""
    if owner != "local" and not re.fullmatch(r"ws_[0-9a-f]{16}", str(owner or "")):
        raise WorkspaceError("工作区需要有效的已验证所有者")
    database = pathlib.Path(database_path).expanduser().resolve()
    return database.parent / "workspaces" / hashlib.sha256(owner.encode("utf-8")).hexdigest()[:32]


def managed_workspace_path(database_path, owner: str, workspace_id: str) -> pathlib.Path:
    if not isinstance(workspace_id, str) or not WORKSPACE_ID_PATTERN.fullmatch(workspace_id):
        raise WorkspaceError("工作区 ID 格式无效")
    return managed_workspaces_root(database_path, owner) / workspace_id


def _validated_bundle(bundle: dict, *, expected_workspace_id: str = "") -> tuple[str, list, list]:
    if not isinstance(bundle, dict) or bundle.get("schema_version") != SCHEMA_VERSION:
        raise WorkspaceError("这不是当前版本可读取的工作区 bundle")
    workspace_id = bundle.get("workspace_id")
    if not isinstance(workspace_id, str) or not WORKSPACE_ID_PATTERN.fullmatch(workspace_id):
        raise WorkspaceError("bundle 的工作区 ID 格式无效")
    if expected_workspace_id and workspace_id != expected_workspace_id:
        raise WorkspaceError("这个 bundle 属于其他工作区；不会合并不同的工作区 ID")
    try:
        bundle_bytes = len(json.dumps(bundle, ensure_ascii=False, separators=(",", ":"),
                                      allow_nan=False).encode("utf-8"))
    except (TypeError, ValueError, RecursionError) as exc:
        raise WorkspaceError("bundle 不是有限大小的 JSON 数据") from exc
    if bundle_bytes > MAX_BUNDLE_BYTES:
        raise WorkspaceError(f"bundle 超过 {MAX_BUNDLE_BYTES // (1024 * 1024)} MiB 上限")
    sources = bundle.get("sources")
    if not isinstance(sources, list) or len(sources) > MAX_BUNDLE_SOURCES:
        raise WorkspaceError("bundle 缺少来源清单或来源数量超过上限")

    valid = []
    conflicts = []
    seen = set()
    for index, item in enumerate(sources):
        if not isinstance(item, dict):
            conflicts.append({"index": index, "reason": "来源记录不是对象"})
            continue
        unknown = sorted(set(item) - WORKSPACE_FIELDS)
        if unknown:
            conflicts.append({"index": index, "reason": "来源包含未知字段，拒绝静默丢弃",
                              "fields": unknown[:20]})
            continue
        if item.get("schema_version") != SCHEMA_VERSION or item.get("origin") not in TRUSTED_ORIGINS:
            conflicts.append({"index": index, "reason": "来源版本不支持或来源不是受信任工具结果"})
            continue
        bad_bound = ((not isinstance(item.get("source_url", ""), str)
                      or len(item.get("source_url", "")) > 800)
                     or (not isinstance(item.get("locator", ""), str)
                         or len(item.get("locator", "")) > 400)
                     or (not isinstance(item.get("kind", ""), str)
                         or len(item.get("kind", "")) > 64)
                     or (not isinstance(item.get("content", ""), str)
                         or len(item.get("content", "").encode("utf-8")) > MAX_SOURCE_BYTES))
        if bad_bound:
            conflicts.append({"index": index, "reason": "来源字段无效或超过大小上限"})
            continue
        try:
            item_size = len(_canonical(item).encode("utf-8"))
        except (TypeError, ValueError, RecursionError):
            item_size = MAX_SOURCE_BYTES + MAX_AUDIT_BYTES + 1
        if item_size > MAX_SOURCE_BYTES + MAX_AUDIT_BYTES:
            conflicts.append({"index": index, "reason": "来源记录超过大小上限"})
            continue
        identifier = source_id(item)
        if identifier in seen:
            conflicts.append({"index": index, "reason": "bundle 中重复出现同一 source ID",
                              "source_id": identifier})
            continue
        seen.add(identifier)
        valid.append((identifier, item))
    return workspace_id, valid, conflicts


class Workspace:
    """A directory the user named. Nothing is stored until a caller records something."""

    def __init__(self, root):
        self.root = pathlib.Path(root).expanduser()
        self._identifier = ""
        self._index: dict[str, dict] = {}

    @property
    def sources_dir(self) -> pathlib.Path:
        return self.root / "sources"

    @property
    def bundle_path(self) -> pathlib.Path:
        return self.root / "bundle.json"

    def open(self, *, create: bool = True, workspace_id: str | None = None) -> "Workspace":
        """Open the selected workspace, optionally without writing anything.

        A new directory gets its identity from an explicitly imported bundle or a random ID. A
        non-empty directory without a valid marker is refused instead of being adopted silently.
        """
        if workspace_id is not None and not WORKSPACE_ID_PATTERN.fullmatch(workspace_id):
            raise WorkspaceError("工作区 ID 格式无效")
        if self.root.is_symlink():
            raise WorkspaceError("工作区目录不能是符号链接")
        if self.root.exists() and not self.root.is_dir():
            raise WorkspaceError("工作区路径不是目录")
        marker = self.root / WORKSPACE_MARKER
        if marker.is_symlink():
            raise WorkspaceError("工作区 marker 不能是符号链接")
        if marker.is_file():
            try:
                if marker.stat().st_size > 4096:
                    raise WorkspaceError("工作区 marker 超过大小上限")
                document = json.loads(marker.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                raise WorkspaceError("工作区 marker 无法读取") from exc
            identifier = document.get("workspace_id", "") if isinstance(document, dict) else ""
            if (document.get("schema_version") != SCHEMA_VERSION if isinstance(document, dict) else True) \
                    or not isinstance(identifier, str) or not WORKSPACE_ID_PATTERN.fullmatch(identifier):
                raise WorkspaceError("工作区 marker 版本或 ID 无效；不会重建覆盖")
            if workspace_id and workspace_id != identifier:
                raise WorkspaceError("工作区 marker 与 bundle 的 ID 不匹配")
            self._identifier = identifier
        else:
            if self.root.exists() and any(self.root.iterdir()):
                raise WorkspaceError("目录非空且没有有效工作区 marker；不会接管现有文件")
            if not create and workspace_id is None:
                raise WorkspaceError("预览新工作区需要 bundle 中的工作区 ID")
            if not create and workspace_id:
                self._identifier = workspace_id
            else:
                self.root.mkdir(parents=True, exist_ok=True)
                self._identifier = workspace_id or ("ws_" + uuid.uuid4().hex[:16])
                marker_payload = json.dumps({"schema_version": SCHEMA_VERSION,
                                             "workspace_id": self._identifier,
                                             "created_at": time.strftime("%Y-%m-%dT%H:%M:%S")},
                                            ensure_ascii=False, indent=2)
                temporary = marker.with_name(f".{WORKSPACE_MARKER}.{uuid.uuid4().hex}.tmp")
                temporary.write_text(marker_payload, encoding="utf-8")
                temporary.replace(marker)
        if create:
            self.root.mkdir(parents=True, exist_ok=True)
            if self.sources_dir.is_symlink():
                raise WorkspaceError("sources 目录不能是符号链接")
            if self.sources_dir.exists() and not self.sources_dir.is_dir():
                raise WorkspaceError("sources 路径不是目录")
            self.sources_dir.mkdir(exist_ok=True)
        if self.sources_dir.exists():
            if self.sources_dir.is_symlink():
                raise WorkspaceError("sources 目录不能是符号链接")
            if not self.sources_dir.is_dir():
                raise WorkspaceError("sources 路径不是目录")
            for path in self.sources_dir.glob("*.json"):
                if ID_PATTERN.fullmatch(path.stem) and path.is_file() and not path.is_symlink():
                    self._index[path.stem] = {"path": path}
        return self

    @property
    def workspace_id(self) -> str:
        if not self._identifier:
            raise WorkspaceError("the workspace is not open; call open() first")
        return self._identifier

    def _snapshot_path(self, identifier: str) -> pathlib.Path:
        if not isinstance(identifier, str) or not ID_PATTERN.match(identifier):
            # An id is a filename here, so anything else is refused before it can traverse.
            raise WorkspaceError(f"not a source id: {identifier!r}")
        return self.sources_dir / f"{identifier}.json"

    def record(self, document: dict, *, tool: str) -> str:
        """Store one document a tool returned, and return its stable id.

        `origin` is written as the tool that produced it. There is no argument for recording model
        text: a workspace holds observations, and a caller cannot relabel one as the other.
        """
        if not isinstance(document, dict):
            raise WorkspaceError("a source must be a mapping")
        if document.get("origin", "tool") not in TRUSTED_ORIGINS:
            raise WorkspaceError("only material a tool returned may be recorded; model text is not "
                                 "evidence and cannot be stored as if it were")
        payload = {
            "schema_version": SCHEMA_VERSION,
            "origin": "tool",
            "tool": str(tool),
            "retrieved_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "source_url": str(document.get("source_url", ""))[:800],
            "locator": str(document.get("locator", ""))[:400],
            "kind": str(document.get("kind", "source"))[:64],
            "content": str(document.get("content", ""))[:MAX_SOURCE_BYTES],
        }
        for key in COPIED_FIELDS:
            if document.get(key) is not None:
                payload[key] = document[key]
        if len(_canonical(payload).encode("utf-8")) > MAX_SOURCE_BYTES + MAX_AUDIT_BYTES:
            raise WorkspaceError("the source is larger than this workspace will store")
        identifier = source_id(payload)
        path = self._snapshot_path(identifier)
        if path.is_symlink():
            raise WorkspaceError("来源快照不能是符号链接")
        if not path.exists():
            # Written beside the destination and moved into place, so an interrupted write cannot
            # leave a half-file that later looks like a recorded source.
            temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
            temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            temporary.replace(path)
        self._index[identifier] = {"path": path}
        return identifier

    def read(self, identifier: str) -> dict:
        path = self._snapshot_path(identifier)
        if path.is_symlink() or not path.is_file():
            # Including the case of an id that belongs to another workspace: it is simply not here.
            raise WorkspaceError(f"this workspace does not hold {identifier}")
        if path.stat().st_size > MAX_SOURCE_BYTES + MAX_AUDIT_BYTES:
            raise WorkspaceError(f"{identifier} exceeds the source size limit")
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise WorkspaceError(f"{identifier} is not a readable source snapshot") from exc
        if (not isinstance(document, dict) or document.get("origin") not in TRUSTED_ORIGINS
                or document.get("schema_version") != SCHEMA_VERSION or source_id(document) != identifier):
            raise WorkspaceError(f"{identifier} does not match its source snapshot")
        return document

    def identifiers(self) -> list[str]:
        return sorted(self._index)

    def bundle(self) -> dict:
        """A versioned export. Import elsewhere presents it as a preview first."""
        bundle = {
            "schema_version": SCHEMA_VERSION,
            "workspace_id": self.workspace_id,
            "exported_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "sources": [self.read(identifier) for identifier in self.identifiers()],
            "note": "sources only; importing never adds a paper to the library. Source origin claims "
                    "are not cryptographically authenticated by this bundle.",
        }
        _validated_bundle(bundle, expected_workspace_id=self.workspace_id)
        return bundle

    @classmethod
    def import_to(cls, root, bundle: dict, *, apply: bool = False) -> tuple["Workspace", dict]:
        """Preview or import a bundle into one explicit directory without touching it on preview."""
        workspace_id, _sources, _conflicts = _validated_bundle(bundle)
        workspace = cls(root).open(create=apply, workspace_id=workspace_id)
        return workspace, workspace.import_bundle(bundle, apply=apply)

    def import_bundle(self, bundle: dict, *, apply: bool = False) -> dict:
        """Preview by default. Idempotent, and it writes nothing unless `apply=True`.

        A bundle from another workspace is refused rather than merged: two workspaces can hold the
        same source id for genuinely different reasons, and folding them together silently would
        make the id stop meaning what it says.
        """
        _workspace_id, sources, validation_conflicts = _validated_bundle(
            bundle, expected_workspace_id=self.workspace_id)
        fresh, present, conflicts = [], [], list(validation_conflicts)
        # Keep validation conflicts visible while continuing with independent valid rows.
        for identifier, item in sources:
            # Identity only, so a re-import of the same bundle stays idempotent.
            known = identifier in self._index
            (present if known else fresh).append(identifier)
            if not known and apply:
                path = self._snapshot_path(identifier)
                payload = dict(item)
                payload["imported_by_user"] = True
                if path.is_symlink():
                    conflicts.append({"reason": "目标 source ID 是符号链接", "source_id": identifier})
                    fresh.remove(identifier)
                    continue
                temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
                temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                                     encoding="utf-8")
                temporary.replace(path)
                self._index[identifier] = {"path": path}
        return {
            "schema_version": SCHEMA_VERSION,
            "applied": bool(apply),
            "workspace_id": self.workspace_id,
            "new": fresh,
            "already_present": present,
            "conflicts": conflicts,
            # Stated explicitly so nobody reads an import as an approval.
            "papers_approved": 0,
            "note": "an import adds sources only; origin claims in a user-supplied bundle are not "
                    "cryptographically authenticated, and paper approval stays a separate, "
                    "human-confirmed action",
            "provenance_verified": False,
        }
