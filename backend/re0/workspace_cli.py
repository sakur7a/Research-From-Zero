"""Export/import one explicitly selected source workspace without touching the paper library."""
from __future__ import annotations

import argparse
import hashlib
import json
import pathlib
import sys
import time
import uuid

from .workspace import MAX_BUNDLE_BYTES, Workspace, WorkspaceError


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _read_bundle(path: pathlib.Path) -> dict:
    if path.is_symlink() or not path.is_file():
        raise WorkspaceError("bundle 输入必须是普通文件")
    if path.stat().st_size > MAX_BUNDLE_BYTES:
        raise WorkspaceError(f"bundle 文件超过 {MAX_BUNDLE_BYTES // (1024 * 1024)} MiB 上限")
    try:
        bundle = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_unique_object)
    except (OSError, UnicodeError, ValueError, RecursionError) as exc:
        raise WorkspaceError(f"bundle 不是有效 JSON：{exc}") from exc
    if not isinstance(bundle, dict):
        raise WorkspaceError("bundle 顶层必须是 JSON 对象")
    return bundle


def _write_bundle(path: pathlib.Path, bundle: dict, *, force: bool) -> dict:
    path = path.expanduser()
    if path.is_symlink():
        raise WorkspaceError("bundle 输出不能是符号链接")
    if path.exists() and not path.is_file():
        raise WorkspaceError("bundle 输出目标必须是普通文件")
    try:
        payload = json.dumps(bundle, ensure_ascii=False, indent=2, allow_nan=False).encode("utf-8")
    except (TypeError, ValueError, RecursionError) as exc:
        raise WorkspaceError("bundle 不能序列化为有限大小的 JSON") from exc
    if len(payload) > MAX_BUNDLE_BYTES:
        raise WorkspaceError(f"bundle 超过 {MAX_BUNDLE_BYTES // (1024 * 1024)} MiB 上限")
    backup = ""
    if path.exists():
        if not force:
            raise WorkspaceError(f"输出文件已存在：{path}。如确认替换，请加 --force（旧文件会保留备份）")
        backup_path = path.with_name(f"{path.name}.re0-backup-{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:8]}")
        path.replace(backup_path)
        backup = str(backup_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_bytes(payload)
        temporary.replace(path)
    except OSError:
        if backup and not path.exists():
            pathlib.Path(backup).replace(path)
        raise
    return {"bundle_path": str(path), "bytes": len(payload),
            "sha256": hashlib.sha256(payload).hexdigest(), "backup": backup}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Move versioned source snapshots between Re0 entry points.")
    actions = parser.add_subparsers(dest="action")
    export = actions.add_parser("export", help="write a source-only bundle from an explicit workspace")
    export.add_argument("--directory", required=True, metavar="DIR",
                        help="the MCP/full-text workspace directory to read")
    export.add_argument("--output", default="", metavar="FILE",
                        help="destination JSON file (default: <workspace>/bundle.json)")
    export.add_argument("--force", action="store_true",
                        help="replace an existing output after renaming it to a backup")
    importer = actions.add_parser("import", help="preview or import a bundle into an explicit workspace")
    importer.add_argument("--directory", required=True, metavar="DIR",
                          help="the destination workspace directory")
    importer.add_argument("--input", required=True, metavar="FILE", help="versioned JSON bundle")
    importer.add_argument("--apply", action="store_true",
                          help="write new source snapshots; omitted means a read-only preview")
    args = parser.parse_args(argv)
    try:
        if args.action == "export":
            workspace = Workspace(args.directory).open(create=False)
            destination = pathlib.Path(args.output).expanduser() if args.output else workspace.bundle_path
            if destination.resolve() == workspace.sources_dir.resolve() or \
                    workspace.sources_dir.resolve() in destination.resolve().parents:
                raise WorkspaceError("bundle 不能写入 sources 子目录")
            result = _write_bundle(destination, workspace.bundle(), force=args.force)
            print(json.dumps({"operation": "export", "workspace_id": workspace.workspace_id,
                              "sources": len(workspace.identifiers()), **result}, ensure_ascii=False))
            return 0
        if args.action == "import":
            bundle = _read_bundle(pathlib.Path(args.input).expanduser())
            workspace, result = Workspace.import_to(args.directory, bundle, apply=args.apply)
            print(json.dumps({"operation": "import", "workspace_id": workspace.workspace_id,
                              **result}, ensure_ascii=False))
            return 0
    except (WorkspaceError, OSError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    parser.print_help(file=sys.stderr)
    return 2
