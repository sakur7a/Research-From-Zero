"""`re0 zotero` — connect one library, preview a sync, and only then commit it.

The key comes from the environment (`ZOTERO_API_KEY`), never from a flag: a command line is visible
in shell history and in the process list, and a bibliographic key is a credential. It is sent to
`api.zotero.org` and nowhere else, is never written to the database, and is never printed — not even
masked, because a masked key is still a partial key.

Every command that reads the remote library is a *preview* unless `--apply` is passed. That is the
same shape the CSL import and the workspace import already use here, and for the same reason: the
caller should see what a sync would touch before it touches anything.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from .deployment import LOCAL_OWNER
from .paths import default_database
from .zotero import Connection, ZoteroError, ZoteroStore
from .zotero_sync import sync

PROGRAM = "re0 zotero"
KEY_ENVIRON = "ZOTERO_API_KEY"
PREVIEW_ROWS = 20


def database_path(explicit: str = "") -> str:
    return explicit or os.getenv("RE0_DB") or str(default_database())


def _stores(path: str):
    from .db import Database
    from .service import Store

    if not Path(path).is_file():
        raise ZoteroError(f"没有文献库数据库：{path}；先启动一次本地服务，或用 --db 指向已有的那一个")
    database = Database(path)
    return Store(database), ZoteroStore(database)


def _connection(arguments, *, needs_key: bool = True) -> Connection:
    key = os.environ.get(KEY_ENVIRON, "").strip()
    if needs_key and not key:
        raise ZoteroError(f"没有 {KEY_ENVIRON}；这个命令要读取远端库，没有凭据就不会假装同步过。"
                          "密钥只发往 api.zotero.org，不会被写进数据库或打印出来。")
    return Connection(library_type=arguments.library_type, library_id=arguments.library_id,
                      api_key=key, label=arguments.label or "",
                      collections=list(arguments.collection or []), tags=list(arguments.tag or []))


def _print_counts(result: dict) -> None:
    counts = result["counts"]
    print("变更统计：")
    for label, key in (("新增", "added"), ("修改", "updated"), ("关联到库内已有条目", "linked_existing"),
                       ("无变化", "unchanged"), ("远端删除（tombstone）", "remote_deleted"),
                       ("跳过", "skipped")):
        print(f"  {label}: {counts[key]}")
    print(f"  远端声明的变更条目: {counts['remote_changes']}；未读取到: {counts['unaccounted']}")
    print(f"游标：{result['cursor']['from']} → {result['cursor']['to']}")


def _print_notes(result: dict) -> None:
    for note in result.get("notes", []):
        print(f"  - {note}")
    for row in result.get("skipped", [])[:PREVIEW_ROWS]:
        print(f"  跳过 {row.get('key', '（无 key）')}：{row.get('reason') or row.get('title', '')}")


def zotero_command(arguments) -> int:
    action = getattr(arguments, "action", "")
    try:
        library, zotero = _stores(database_path(arguments.db))
        if action == "status":
            status = zotero.status(_connection(arguments, needs_key=False), owner=LOCAL_OWNER)
            print(f"库：{status['connection']['library_type']} {status['connection']['library_id']}"
                  f"{'（' + status['connection']['label'] + '）' if status['connection']['label'] else ''}")
            cursor = status["cursor"]
            print(f"游标：已提交版本 {cursor['committed_version']}；{cursor.get('note', '')}")
            print(f"映射：{status['links'] or '无'}；对应本地论文 {status['distinct_papers']} 篇")
            if status["collections"]:
                print("集合：")
                for row in status["collections"]:
                    print(f"  {'[已选]' if row['selected'] else '[未选]'} {row['key']} {row['name']}")
            if status["recent_syncs"]:
                print("最近同步：")
                for row in status["recent_syncs"]:
                    counts = row.get("counts") or {}
                    print(f"  {row['at']} {'已提交' if row['applied'] else '预览'} "
                          f"新增 {counts.get('added', 0)} 修改 {counts.get('updated', 0)} "
                          f"远端删除 {counts.get('remote_deleted', 0)} 跳过 {counts.get('skipped', 0)}")
            print()
            print("状态里没有 API key，也没有笔记或附件内容：这里只保存映射、游标与集合选择。")
            return 0
        if action == "collections":
            from .zotero import ZoteroClient
            connection = _connection(arguments)
            with ZoteroClient(connection, max_requests=10) as client:
                rows = client.collections()
            written = zotero.save_collections(connection, rows, owner=LOCAL_OWNER,
                                              selected=list(arguments.collection or []) or None)
            print(f"读到 {written} 个集合（只读名称与 key，没有读取集合内容）：")
            for row in rows[:60]:
                print(f"  {row['key']}  {row['name']}")
            if arguments.collection:
                print(f"已选中：{'、'.join(arguments.collection)}")
            else:
                print("没有改动已有的选择。用 `re0 zotero select --collection KEY` 选择要同步的集合。")
            return 0
        if action == "select":
            result = zotero.select_collections(_connection(arguments, needs_key=False),
                                               list(arguments.collection or []),
                                               owner=LOCAL_OWNER)
            print(f"已选中 {len(result['selected'])} 个集合。{result['note']}")
            return 0
        if action in {"preview", "sync"}:
            connection = _connection(arguments)
            apply = action == "sync" and bool(arguments.apply)
            result = sync(zotero, library.list_papers(owner=LOCAL_OWNER), connection,
                          owner=LOCAL_OWNER, apply=apply, max_requests=arguments.max_requests)
            print(("已提交同步" if result.get("applied") else "预览（没有写入任何内容）"))
            _print_counts(result)
            print()
            for row in result.get("preview", [])[:PREVIEW_ROWS]:
                print(f"  [{row['matched_on'] or 'new'}] {row['key']} {row['title'][:80]}"
                      f"{' · ' + row['doi'] if row['doi'] else ''}")
            if result["counts"]["added"] + result["counts"]["updated"] \
                    + result["counts"]["linked_existing"] > PREVIEW_ROWS:
                print(f"  …其余未列出（共 {result['counts']['added']} 新增 / "
                      f"{result['counts']['updated']} 修改 / "
                      f"{result['counts']['linked_existing']} 关联）")
            print()
            print("说明：")
            _print_notes(result)
            print()
            if result.get("applied"):
                print(f"本次用了 {result.get('requests_used', '?')} 个请求。映射与游标在同一个事务里写入。")
            else:
                print("这是预览：文献库没有任何改动，游标没有移动。加 --apply 才会提交。")
            if arguments.json:
                Path(arguments.json).write_text(
                    json.dumps({key: value for key, value in result.items()
                                if key not in ("added", "updated", "linked_existing")},
                               ensure_ascii=False, indent=2), encoding="utf-8")
                print(f"wrote {arguments.json}（不含 API key）")
            return 0
        if action == "disconnect":
            result = zotero.disconnect(_connection(arguments, needs_key=False),
                                       owner=LOCAL_OWNER,
                                       remove_links=bool(arguments.remove_links))
            print(f"已断开。删除映射 {result['removed_links']} 条；文献库仍有 "
                  f"{result['papers_remaining']} 篇论文。")
            print(result["note"])
            return 0
    except ZoteroError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except Exception as exc:  # noqa: BLE001 - reported as a status, never as a traceback or a key
        from fastapi import HTTPException
        if isinstance(exc, HTTPException):
            print(f"未完成：{exc.detail}", file=sys.stderr)
            return 1
        print(f"未完成（{type(exc).__name__}）；文献库没有部分写入。", file=sys.stderr)
        return 1
    print("pass one of: status, collections, select, preview, sync --apply, disconnect",
          file=sys.stderr)
    return 2


def _library_arguments(parser, *, needs_selection: bool = True) -> None:
    parser.add_argument("--library-type", choices=("user", "group"), default="user")
    parser.add_argument("--library-id", required=True,
                        help="numeric Zotero user ID or group ID")
    parser.add_argument("--label", default="", help="a name for this connection, stored locally")
    if needs_selection:
        parser.add_argument("--collection", action="append", default=[], metavar="KEY",
                            help="restrict the sync to these collections; repeatable, all of them "
                                 "are sent in one request")
        parser.add_argument("--tag", action="append", default=[], metavar="NAME",
                            help="restrict the sync to these tags; repeatable, matched as a union "
                                 "(an item carrying any of them is read)")
    parser.add_argument("--db", default="", help="library database path (default: $RE0_DB)")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog=PROGRAM, description=__doc__.splitlines()[0])
    actions = parser.add_subparsers(dest="action")

    status = actions.add_parser("status", help="mappings, cursor and collection selection; no network")
    _library_arguments(status, needs_selection=False)

    collections = actions.add_parser("collections", help="read the remote collection list (names only)")
    _library_arguments(collections)

    select = actions.add_parser("select", help="choose which stored collections a sync covers")
    _library_arguments(select, needs_selection=False)
    select.add_argument("--collection", action="append", default=[], metavar="KEY")

    for name, help_text in (("preview", "read the remote library and report what would change"),
                            ("sync", "preview, and with --apply commit it in one transaction")):
        sub = actions.add_parser(name, help=help_text)
        _library_arguments(sub)
        sub.add_argument("--max-requests", type=int, default=60)
        sub.add_argument("--json", default="", metavar="PATH", help="write the summary here (no key)")
        if name == "sync":
            sub.add_argument("--apply", action="store_true",
                             help="actually write. Without it, `sync` is a preview.")

    disconnect = actions.add_parser("disconnect", help="forget the cursor and selection; keep the papers")
    _library_arguments(disconnect, needs_selection=False)
    disconnect.add_argument("--remove-links", action="store_true",
                            help="also drop the remote mappings. Papers, notes, resources and "
                                 "evidence are never deleted either way.")
    return parser


def main(argv=None) -> int:
    parser = build_parser()
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        parser.print_help()
        return 2
    try:
        arguments = parser.parse_args(argv)
    except SystemExit as exc:
        return int(exc.code or 0)
    if not getattr(arguments, "action", ""):
        parser.print_help()
        return 2
    return zotero_command(arguments)
