"""Preview, then commit: the two halves of a Zotero sync, kept apart on purpose.

`sync(..., apply=False)` reads the remote library and reports exactly what would change without
writing anything. `sync(..., apply=True)` reads again and writes what it saw. Fetching twice is a
deliberate trade: it means the plan that is applied is the plan that was actually observed, and no
remote payload has to be held in server memory between two requests. A sync window after the first
run is normally a handful of items, so the second read is cheap, and the cursor check refuses to
apply a plan whose starting point has moved.

Two invariants hold across both halves:

* **The cursor moves inside the same transaction as the links.** An interrupted sync therefore
  re-reads the same window; it cannot commit the cursor and lose the items, or write items and leave
  the cursor behind.
* **Nothing a reader owns is written.** A sync may set bibliographic fields. `notes`, `topics` and
  reading `status` are never in the write set, so a remote edit cannot overwrite an annotation.
"""
from __future__ import annotations

import json
import re
import sqlite3
from uuid import uuid4

from fastapi import HTTPException

from .db import encode
from .knowledge import (append_source_snapshot, ensure_paper_version, metadata_snapshot,
                        sync_topic_assignments)
from .models import now
from .scheduling import Governor
from .zotero import (BIBLIOGRAPHIC_ITEM_TYPES, TITLE_KEY_MIN_CHARS, Connection, ZoteroClient,
                     ZoteroError, ZoteroStore, remote_item, title_key)

PREVIEW_ITEMS = 20
# A change the request did not ask for: an attachment, a note, an annotation, or an item type not on
# the inclusion list. Counted and reported, because a narrow request must not read as a complete one.
UNACCOUNTED_NOTE = ("变更列表里有 {count} 个条目不在本次读取范围内（附件、笔记、批注，或未列入 "
                    "{types} 的类型）。它们没有被读取，也没有被计入新增或修改。")


def _paper_index(papers: list[dict]) -> dict:
    by_doi, by_arxiv, by_title = {}, {}, {}
    for paper in papers:
        doi = (paper.get("doi") or "").strip().lower()
        if doi:
            by_doi.setdefault(doi, paper["id"])
        base = re.sub(r"v\d+$", "", paper.get("arxiv_id") or "").strip().lower()
        if base:
            by_arxiv.setdefault(base, paper["id"])
        key = title_key(paper.get("title") or "")
        if key:
            by_title.setdefault(key, []).append(paper["id"])
    return {"doi": by_doi, "arxiv": by_arxiv, "title": by_title,
            "by_id": {paper["id"]: paper for paper in papers}}


def _match(item, index: dict) -> tuple[str, str]:
    """Which paper this remote item is the same work as, and what it was matched on.

    Identity stays `(library_type, library_id, item_key)`; DOI and arXiv only *associate*. An empty
    id means "no paper here yet".
    """
    doi = (item.doi or "").strip().lower()
    if doi and doi in index["doi"]:
        return index["doi"][doi], "doi"
    base = re.sub(r"v\d+$", "", item.arxiv_id or "").strip().lower()
    if base and base in index["arxiv"]:
        return index["arxiv"][base], "arxiv"
    key = title_key(item.title)
    candidates = index["title"].get(key, []) if key else []
    if len(candidates) == 1:
        return candidates[0], "title"
    if len(candidates) > 1:
        # Two library records with the same title are not interchangeable, and guessing which one a
        # remote item means would attach a mapping to the wrong paper permanently.
        return "", "ambiguous_title"
    return "", ""


def plan(store: ZoteroStore, papers: list[dict], connection: Connection, *, owner: str,
         client: ZoteroClient) -> dict:
    """Read the remote library and classify every change. Writes nothing.

    `papers` and `owner` must agree: the caller passes one owner's library, and every mapping this
    plan reads or writes belongs to that owner. Two readers syncing the same Zotero library therefore
    get two plans and two cursors, which is the point — one reader's "unchanged" is not the other's.
    """
    cursor = store.cursor(connection, owner=owner)
    since = int(cursor.get("committed_version") or 0)
    selected = connection.collections or [
        row["key"] for row in store.status(connection, owner=owner)["collections"] if row["selected"]]
    remote_version = client.library_version()
    changed = client.changed_versions(since)
    deleted = set(client.deleted_keys(since))
    raw_items, page_info = client.items(since, collections=selected, tags=connection.tags)

    index = _paper_index(papers)
    added, updated, linked, unchanged, skipped, tombstoned = [], [], [], [], [], []
    seen: set[str] = set()
    # Identifiers claimed by an `added` entry in this same plan. A second remote item carrying the
    # same DOI cannot also be added — the library's unique index would reject it and roll the whole
    # transaction back, so one duplicate in the remote library would abort every other change.
    claimed_doi: dict[str, str] = {}
    claimed_arxiv: dict[str, str] = {}
    short_titles = 0
    for raw in raw_items:
        item = remote_item(raw)
        if item is None:
            skipped.append({"reason": "不是可同步的文献条目（附件、笔记或批注一律不读取）"})
            continue
        if item.key in seen:
            continue
        seen.add(item.key)
        existing = store.link(connection, item.key, owner=owner)
        if existing and existing["state"] == "linked" and existing["remote_version"] >= item.version \
                and existing["paper_id"] in index["by_id"]:
            unchanged.append({"key": item.key, "title": item.title, "version": item.version})
            continue
        doi = (item.doi or "").strip().lower()
        base = re.sub(r"v\d+$", "", item.arxiv_id or "").strip().lower()
        if existing and existing["paper_id"] in index["by_id"]:
            target, matched_on = existing["paper_id"], "remote_identity"
        else:
            target, matched_on = _match(item, index)
        if matched_on == "ambiguous_title":
            skipped.append({"key": item.key, "title": item.title,
                            "reason": "标题与库内多条记录相同，无法确定对应哪一条；没有猜测"})
            continue
        if not target and not doi and not base and not title_key(item.title):
            # Too short to match on safely, so it becomes a new record. Said out loud, because the
            # alternative is a duplicate the reader has to find by hand.
            short_titles += 1
        if doi and doi in claimed_doi:
            skipped.append({"key": item.key, "title": item.title,
                            "reason": f"本次同步中 {claimed_doi[doi]} 已占用 DOI {item.doi}；"
                                     "没有为同一个 DOI 建两条记录"})
            continue
        if base and base in claimed_arxiv:
            skipped.append({"key": item.key, "title": item.title,
                            "reason": f"本次同步中 {claimed_arxiv[base]} 已占用 arXiv {item.arxiv_id}"})
            continue
        entry = {"key": item.key, "version": item.version, "title": item.title,
                 "item_type": item.item_type, "doi": item.doi, "arxiv_id": item.arxiv_id,
                 "matched_on": matched_on, "paper_id": target,
                 "previous_version": int(existing["remote_version"]) if existing else 0,
                 "was_tombstoned": bool(existing and existing["state"] == "remote_deleted"),
                 "record": item.paper(existing=index["by_id"].get(target)).model_dump(mode="json")}
        holder = index["doi"].get(doi) if doi else None
        if holder and target and holder != target:
            # This happens when an already-linked item's DOI is edited remotely to one another paper
            # here already holds. Writing it would break the unique index, so neither side is touched.
            skipped.append({"key": item.key, "title": item.title,
                            "reason": f"DOI {item.doi} 已属于库内另一条记录；没有覆盖任何一方"})
            continue
        if target:
            (updated if existing else linked).append(entry)
        else:
            added.append(entry)
            if doi:
                claimed_doi[doi] = item.key
            if base:
                claimed_arxiv[base] = item.key

    for key in sorted(deleted):
        existing = store.link(connection, key, owner=owner)
        if existing and existing["state"] == "linked":
            paper = index["by_id"].get(existing["paper_id"]) or {}
            tombstoned.append({"key": key, "paper_id": existing["paper_id"],
                               "title": paper.get("title", ""),
                               "reason": "远端已删除；标记为 tombstone，本地记录、笔记、资源与证据都保留"})
        else:
            skipped.append({"key": key, "reason": "远端删除，但本地没有对应映射，无需处理"})

    accounted = len(seen) + len(deleted)
    unaccounted = max(0, len(changed) - accounted)
    notes = list(page_info.get("scope_notes") or [])
    if unaccounted:
        notes.append(UNACCOUNTED_NOTE.format(count=unaccounted,
                                             types="、".join(BIBLIOGRAPHIC_ITEM_TYPES[:6]) + " 等"))
    if page_info["stops"]:
        notes.append("分页在读完之前停止了（%s）；本次不同步，因为漏掉的变更会被当成没有变化"
                     % "、".join(page_info["stops"]))
    if since == 0:
        notes.append("这是该库的第一次同步，游标从 0 开始，读取的是当前全部符合条件的条目")
    if short_titles:
        notes.append("有 %d 条远端条目既没有 DOI/arXiv，标题归一化后又短于 %d 字符，无法安全地与库内"
                     "记录关联；它们会作为新条目加入，可能与库内已有记录重复"
                     % (short_titles, TITLE_KEY_MIN_CHARS))
    if skipped:
        notes.append("本次有 %d 条被跳过，原因逐条写在 skipped 里并记入同步日志；游标仍会前进，"
                     "所以这些条目不会自动重试，要等远端再次变更才会重新出现" % len(skipped))
    return {
        "schema_version": "1",
        "applied": False,
        "connection": connection.public(),
        "cursor": {"from": since, "to": remote_version},
        "counts": {"added": len(added), "updated": len(updated), "linked_existing": len(linked),
                   "unchanged": len(unchanged), "remote_deleted": len(tombstoned),
                   "skipped": len(skipped), "unaccounted": unaccounted,
                   "remote_changes": len(changed)},
        "added": added, "updated": updated, "linked_existing": linked, "unchanged": unchanged,
        "remote_deleted": tombstoned, "skipped": skipped,
        "preview": [{"key": entry["key"], "title": entry["title"], "item_type": entry["item_type"],
                     "doi": entry["doi"], "matched_on": entry["matched_on"]}
                    for entry in (added + updated + linked)[:PREVIEW_ITEMS]],
        "pages": page_info, "notes": notes,
        "scope": page_info.get("scope") or {},
        "note": "这是预览：没有写入任何内容，游标没有移动。取消同步等于什么都不做。",
    }


def commit(store: ZoteroStore, plan: dict, connection: Connection, *, owner: str) -> dict:
    """Write one plan in a single transaction, cursor included.

    Everything or nothing: a collision, a vanished paper or a crash part-way leaves the library
    exactly as it was, with the cursor still pointing at the last committed version. Every row
    written carries the owner the plan was made for, and the cursor precondition is read from
    that owner's row: another reader of the same Zotero library has their own cursor, so their
    progress can neither invalidate nor be invalidated by this plan.
    """
    kind, identifier = connection.identity
    expected = int(plan["cursor"]["from"])
    target_version = int(plan["cursor"]["to"])
    written = {"added": [], "updated": [], "linked_existing": [], "remote_deleted": []}
    try:
        with store.db.connect() as con:
            con.execute("BEGIN IMMEDIATE")
            row = con.execute("SELECT committed_version FROM zotero_cursors WHERE owner=? AND "
                              "library_type=? AND library_id=?",
                              (owner, kind, identifier)).fetchone()
            if (int(row[0]) if row else 0) != expected:
                raise HTTPException(409, "同步计划已过期：游标已经移动，请重新预览后再确认")
            stamp = now()
            for entry in plan["added"]:
                paper_id = str(uuid4())
                record = entry["record"]
                _insert_paper(con, paper_id, record, stamp, owner)
                _upsert_link(con, owner, kind, identifier, entry, paper_id, stamp)
                written["added"].append({"key": entry["key"], "paper_id": paper_id,
                                         "title": record.get("title", "")})
            for entry in plan["updated"]:
                _update_paper(con, entry["paper_id"], entry["record"], stamp, owner)
                _upsert_link(con, owner, kind, identifier, entry, entry["paper_id"], stamp)
                written["updated"].append({"key": entry["key"], "paper_id": entry["paper_id"],
                                           "title": entry["record"].get("title", ""),
                                           "version": f"{entry['previous_version']} → {entry['version']}"})
            for entry in plan["linked_existing"]:
                _upsert_link(con, owner, kind, identifier, entry, entry["paper_id"], stamp)
                written["linked_existing"].append({"key": entry["key"], "paper_id": entry["paper_id"],
                                                   "matched_on": entry["matched_on"]})
            for entry in plan["remote_deleted"]:
                con.execute("UPDATE zotero_links SET state='remote_deleted', synced_at=? WHERE "
                            "owner=? AND library_type=? AND library_id=? AND item_key=?",
                            (stamp, owner, kind, identifier, entry["key"]))
                written["remote_deleted"].append({"key": entry["key"], "paper_id": entry["paper_id"]})
            con.execute("INSERT INTO zotero_cursors(owner,library_type,library_id,committed_version,"
                        "planned_version,label,scope,updated_at) VALUES (?,?,?,?,?,?,?,?) "
                        "ON CONFLICT(owner,library_type,library_id) DO UPDATE SET "
                        "committed_version=excluded.committed_version, label=excluded.label, "
                        "scope=excluded.scope, updated_at=excluded.updated_at",
                        (owner, kind, identifier, target_version, target_version,
                         connection.label[:200],
                         # The scope as it was applied, not as it was asked for: `scope_filters`
                         # deduplicates and clamps, and a stored scope that disagreed with the
                         # parameters actually sent would make `status` describe a different sync.
                         encode(plan.get("scope") or {}), stamp))
            con.execute("INSERT INTO zotero_sync_log(owner,library_type,library_id,at,applied,data) "
                        "VALUES (?,?,?,?,1,?)",
                        (owner, kind, identifier, stamp, encode({"counts": plan["counts"],
                                                                 "cursor": plan["cursor"],
                                                                 "notes": plan["notes"],
                                                                 "skipped": plan["skipped"][:50]})))
    except sqlite3.IntegrityError as exc:
        raise HTTPException(409, f"写入被数据库拒绝，本次同步没有任何改动：{exc}") from exc
    return {"applied": True, "connection": connection.public(), "cursor": plan["cursor"],
            "counts": plan["counts"], "written": written,
            "note": "已提交：映射、条目与游标在同一个事务里写入。远端删除只是 tombstone，"
                    "本地笔记、资源与证据没有被删除。"}


def _insert_paper(con, paper_id: str, record: dict, stamp: str, owner: str) -> None:
    con.execute("INSERT INTO papers(id,data,doi,arxiv_base,created_at,updated_at,is_demo,owner) "
                "VALUES (?,?,?,?,?,?,0,?)",
                (paper_id, encode(record), record.get("doi", ""),
                 re.sub(r"v\d+$", "", record.get("arxiv_id") or ""), stamp, stamp, owner))
    con.executemany("INSERT OR IGNORE INTO topics(name,owner) VALUES (?,?)",
                    [(name, owner) for name in record.get("topics") or []])
    version_id = ensure_paper_version(con, paper_id, owner, record, origin="zotero_sync",
                                      created_at=stamp)
    append_source_snapshot(con, work_id=paper_id, owner=owner,
                           paper_version_id=version_id, kind="zotero_metadata",
                           payload=metadata_snapshot(record), retrieved_at=stamp,
                           source_url=record.get("paper_url") or "", locator="Zotero item metadata")
    sync_topic_assignments(con, paper_id, owner, record.get("topics") or [], stamp)


def _update_paper(con, paper_id: str, record: dict, stamp: str, owner: str) -> None:
    """Overwrite the bibliographic fields only.

    The stored row is read back and merged, so `notes`, `topics` and `status` — which the remote
    payload has never seen — survive an update instead of being reset to defaults. The owner is in
    the WHERE clause of both statements, so a plan naming a paper this owner does not have writes
    nothing rather than writing into somebody else's record.
    """
    row = con.execute("SELECT data FROM papers WHERE id=? AND owner=?",
                      (paper_id, owner)).fetchone()
    if row is None:
        raise HTTPException(409, "计划里的论文在提交前被删除了；本次同步没有写入任何内容")
    stored = json.loads(row[0])
    before = metadata_snapshot(stored)
    merged = {**stored, **{key: value for key, value in record.items()
                           if key not in ("notes", "topics", "status")}}
    con.execute("UPDATE papers SET data=?,doi=?,arxiv_base=?,updated_at=? WHERE id=? AND owner=?",
                (encode(merged), merged.get("doi", ""),
                 re.sub(r"v\d+$", "", merged.get("arxiv_id") or ""), stamp, paper_id, owner))
    version_id = ensure_paper_version(con, paper_id, owner, merged, origin="zotero_sync",
                                      created_at=stamp)
    current_metadata = metadata_snapshot(merged)
    if before != current_metadata:
        append_source_snapshot(con, work_id=paper_id, owner=owner,
                               paper_version_id=version_id, kind="zotero_metadata",
                               payload=current_metadata, retrieved_at=stamp,
                               source_url=merged.get("paper_url") or "", locator="Zotero item update")
    sync_topic_assignments(con, paper_id, owner, merged.get("topics") or [], stamp)


def _upsert_link(con, owner: str, kind: str, identifier: str, entry: dict, paper_id: str,
                 stamp: str) -> None:
    con.execute("INSERT INTO zotero_links(owner,library_type,library_id,item_key,paper_id,"
                "remote_version,state,item_type,synced_at) VALUES (?,?,?,?,?,?,'linked',?,?) "
                "ON CONFLICT(owner,library_type,library_id,item_key) DO UPDATE SET "
                "paper_id=excluded.paper_id, remote_version=excluded.remote_version, "
                "state='linked', item_type=excluded.item_type, synced_at=excluded.synced_at",
                (owner, kind, identifier, entry["key"], paper_id, int(entry["version"]),
                 entry.get("item_type", ""), stamp))


def sync(store: ZoteroStore, papers: list[dict], connection: Connection, *, owner: str,
         transport=None, apply: bool = False, max_requests: int = 60) -> dict:
    """One sync: preview by default, and only `apply=True` writes.

    The key lives on the connection object for the length of this call and is never stored, logged or
    returned; `connection.public()` is what every response carries.
    """
    try:
        with ZoteroClient(connection, transport=transport,
                          governor=Governor(max_requests=max_requests, seconds=180),
                          max_requests=max_requests) as client:
            result = plan(store, papers, connection, owner=owner, client=client)
            result["requests_used"] = client.requests
    except ZoteroError as exc:
        raise HTTPException(502, str(exc)) from exc
    if result["pages"]["stops"]:
        # An incomplete read is not a smaller sync; committing it would move the cursor past changes
        # nobody saw, and the next run would treat them as unchanged forever.
        raise HTTPException(502, "分页没有读完（%s）；游标未移动，本次没有写入"
                            % "、".join(result["pages"]["stops"]))
    if not apply:
        return result
    applied = commit(store, result, connection, owner=owner)
    return {**result, **applied}
