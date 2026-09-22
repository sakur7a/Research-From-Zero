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
WORKSPACE_MARKER = ".re0-workspace.json"
MAX_SOURCE_BYTES = 256 * 1024
MAX_BUNDLE_SOURCES = 2000
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


def _identity(payload: dict) -> dict:
    return {key: payload[key] for key in IDENTITY_FIELDS if payload.get(key) is not None}


def source_id(payload: dict) -> str:
    """Content-addressed over the identity fields, so the same source always has the same id."""
    return "src_" + hashlib.sha256(_canonical(_identity(payload)).encode("utf-8")).hexdigest()[:16]


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

    def open(self) -> "Workspace":
        """Create or adopt the directory. A workspace gets an identity of its own so a bundle from
        another one can be recognised and refused."""
        self.root.mkdir(parents=True, exist_ok=True)
        marker = self.root / WORKSPACE_MARKER
        if marker.is_file():
            try:
                self._identifier = json.loads(marker.read_text(encoding="utf-8")).get("workspace_id", "")
            except ValueError:
                self._identifier = ""
        if not self._identifier:
            self._identifier = "ws_" + uuid.uuid4().hex[:16]
            marker.write_text(json.dumps({"schema_version": SCHEMA_VERSION,
                                          "workspace_id": self._identifier,
                                          "created_at": time.strftime("%Y-%m-%dT%H:%M:%S")},
                                         ensure_ascii=False, indent=2), encoding="utf-8")
        self.sources_dir.mkdir(exist_ok=True)
        for path in self.sources_dir.glob("*.json"):
            if ID_PATTERN.match(path.stem):
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
        for key in ("paper", "publication", "preprint_also", "institutions",
                    "artifact_candidates", "artifact_search"):
            if document.get(key) is not None:
                payload[key] = document[key]
        if len(_canonical(payload).encode("utf-8")) > MAX_SOURCE_BYTES + 8192:
            raise WorkspaceError("the source is larger than this workspace will store")
        identifier = source_id(payload)
        path = self._snapshot_path(identifier)
        if not path.exists():
            # Written beside the destination and moved into place, so an interrupted write cannot
            # leave a half-file that later looks like a recorded source.
            temporary = path.with_suffix(".tmp")
            temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            temporary.replace(path)
        self._index[identifier] = {"path": path}
        return identifier

    def read(self, identifier: str) -> dict:
        path = self._snapshot_path(identifier)
        if not path.is_file():
            # Including the case of an id that belongs to another workspace: it is simply not here.
            raise WorkspaceError(f"this workspace does not hold {identifier}")
        return json.loads(path.read_text(encoding="utf-8"))

    def identifiers(self) -> list[str]:
        return sorted(self._index)

    def bundle(self) -> dict:
        """A versioned export. Import elsewhere presents it as a preview first."""
        return {
            "schema_version": SCHEMA_VERSION,
            "workspace_id": self.workspace_id,
            "exported_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "sources": [self.read(identifier) for identifier in self.identifiers()],
            "note": "sources only; importing this never adds a paper to the library",
        }

    def import_bundle(self, bundle: dict, *, apply: bool = False) -> dict:
        """Preview by default. Idempotent, and it writes nothing unless `apply=True`.

        A bundle from another workspace is refused rather than merged: two workspaces can hold the
        same source id for genuinely different reasons, and folding them together silently would
        make the id stop meaning what it says.
        """
        if not isinstance(bundle, dict) or bundle.get("schema_version") != SCHEMA_VERSION:
            raise WorkspaceError("this bundle is not a workspace bundle of a version we can read")
        if bundle.get("workspace_id") != self.workspace_id:
            raise WorkspaceError("this bundle belongs to a different workspace; import it into that "
                                 "workspace or start a new one, rather than merging the two")
        sources = bundle.get("sources")
        if not isinstance(sources, list) or len(sources) > MAX_BUNDLE_SOURCES:
            raise WorkspaceError("the bundle's source list is missing or larger than we accept")
        fresh, present, conflicts = [], [], []
        for item in sources:
            if not isinstance(item, dict):
                conflicts.append({"reason": "not a source record"})
                continue
            if item.get("origin") not in TRUSTED_ORIGINS:
                conflicts.append({"reason": "origin is not a tool", "locator": item.get("locator", "")})
                continue
            # Identity only, so a re-import of the same bundle stays idempotent.
            identifier = source_id(item)
            known = identifier in self._index
            (present if known else fresh).append(identifier)
            if not known and apply:
                path = self._snapshot_path(identifier)
                path.write_text(json.dumps(item, ensure_ascii=False, indent=2), encoding="utf-8")
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
            "note": "an import adds sources; approving a paper into the library stays a separate, "
                    "human-confirmed action",
        }
