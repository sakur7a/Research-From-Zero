"""Zotero read-only incremental sync, against fixtures: no account, no network, no key that works.

Every acceptance case in the issue has a fixture here, including the ones that are easy to get
wrong in a way that looks like success: a no-change sync that duplicates, a remote deletion that
cascades, a cursor that moves past items nobody committed, and a narrow request that reads as a
complete one.
"""
import json
import re

import httpx
import pytest

from re0.db import Database
from re0.models import PaperInput
from re0.service import Store
from re0.zotero import (BIBLIOGRAPHIC_ITEM_TYPES, Connection, ZoteroError, ZoteroStore,
                        library_prefix, remote_item)
from re0.zotero_sync import commit, plan, sync

KEY = "zotero-fixture-key-not-a-secret-but-not-real"
PREFIX = "/users/12345"
GROUP_PREFIX = "/groups/777"


def item(key, version, title, *, doi="", extra="", url="", item_type="journalArticle",
         collections=(), tags=(), abstract=""):
    return {"key": key, "version": version, "itemType": item_type,
            "data": {"key": key, "version": version, "itemType": item_type, "title": title,
                     "creators": [{"firstName": "测试", "lastName": "作者"}], "date": "2025-03-04",
                     "DOI": doi, "extra": extra, "url": url, "abstractNote": abstract,
                     "publicationTitle": "Fixture Conference", "collections": list(collections),
                     "tags": [{"tag": name} for name in tags]}}


class ZoteroService:
    """Serves one library's endpoints and records every request, headers included."""

    def __init__(self, items=None, versions=None, deleted=(), total=None, library_version=300,
                 short_page=False, prefix=PREFIX):
        self.items = items if items is not None else []
        self.versions = versions if versions is not None else {row["key"]: row["version"]
                                                              for row in self.items}
        self.deleted = list(deleted)
        self.total = total if total is not None else len(self.items)
        self.library_version = library_version
        self.short_page = short_page
        self.prefix = prefix
        self.requests = []

    def __call__(self, request):
        self.requests.append(request)
        assert request.method == "GET", "this connector must never write to Zotero"
        assert request.headers["zotero-api-key"] == KEY
        assert request.headers["zotero-api-version"] == "3"
        path, query = request.url.path, request.url.params
        if path == self.prefix and query.get("format") == "versions":
            return httpx.Response(200, json={},
                                  headers={"Last-Modified-Version": str(self.library_version)})
        if path == self.prefix + "/deleted":
            return httpx.Response(200, json={"items": self.deleted, "collections": [], "tags": [],
                                             "searches": []})
        if path == self.prefix + "/items" and query.get("format") == "versions":
            return httpx.Response(200, json=self.versions,
                                  headers={"Last-Modified-Version": str(self.library_version)})
        if path == self.prefix + "/items" and query.get("format") == "json":
            start = int(query.get("start", 0))
            limit = int(query.get("limit", 50))
            # A short page with a Total-Results that promises more is how an interrupted read looks.
            page = [] if self.short_page else self.items[start:start + limit]
            return httpx.Response(200, json=page,
                                  headers={"Total-Results": str(self.total),
                                           "Last-Modified-Version": str(self.library_version)})
        if path == self.prefix + "/collections":
            return httpx.Response(200, json=[{"key": "COLL0001", "version": 12,
                                              "data": {"key": "COLL0001", "name": "图层分解"}}])
        raise AssertionError(f"unexpected Zotero request: {path}?{request.url.query}")


def connection(**extra) -> Connection:
    return Connection(library_type="user", library_id="12345", api_key=KEY,
                      label="fixture 用户库", **extra)


def stores(tmp_path):
    database = Database(str(tmp_path / "zotero.sqlite3"))
    return Store(database), ZoteroStore(database)


def _client(service, conn=None):
    """A real ZoteroClient over a mock transport, with the shared budget a sync would use."""
    from re0.scheduling import Governor
    from re0.zotero import ZoteroClient
    return ZoteroClient(conn or connection(), transport=httpx.MockTransport(service),
                        governor=Governor(max_requests=40, seconds=60), max_requests=40)


# ---------------------------------------------------------------------------------- pure functions

def test_library_identity_is_validated_before_it_reaches_a_url():
    assert library_prefix("user", "12345") == "/users/12345"
    assert library_prefix("group", "99") == "/groups/99"
    for bad in ("../../etc", "12345/items", "", "abc", "1" * 40):
        with pytest.raises(ZoteroError):
            library_prefix("user", bad)
    with pytest.raises(ZoteroError):
        library_prefix("robots", "12345")


def test_attachments_notes_and_annotations_are_not_normalised_into_records():
    assert remote_item(item("ATTACH01", 5, "PDF", item_type="attachment")) is None
    assert remote_item(item("NOTE0001", 5, "我的私人笔记", item_type="note")) is None
    assert remote_item(item("ANN000001", 5, "批注", item_type="annotation")) is None
    assert remote_item({"key": "not-a-key", "data": {"title": "x", "itemType": "book"}}) is None
    assert remote_item(item("BOOK0001", 5, "")) is None
    kept = remote_item(item("JOUR0001", 7, "Fixture Title", doi="10.1000/xyz",
                            extra="arXiv:2312.00286v1"))
    assert kept.doi == "10.1000/xyz" and kept.arxiv_id == "2312.00286v1" and kept.version == 7
    assert kept.creators == ["测试 作者"] and kept.venue == "Fixture Conference"


def test_the_inclusion_list_never_names_a_type_whose_body_is_private():
    assert "attachment" not in BIBLIOGRAPHIC_ITEM_TYPES
    assert "note" not in BIBLIOGRAPHIC_ITEM_TYPES
    assert "annotation" not in BIBLIOGRAPHIC_ITEM_TYPES
    assert len(BIBLIOGRAPHIC_ITEM_TYPES) > 20


# ------------------------------------------------------------------------------------- first sync

def test_a_first_sync_previews_then_commits_and_moves_the_cursor_once(tmp_path):
    service = ZoteroService(items=[item("AAAA0001", 300, "Layered Canvas Decomposition",
                                        doi="10.1000/a", abstract="fixture abstract"),
                                   item("BBBB0002", 300, "Constraint-first Layout Generation",
                                        extra="arXiv:2401.00002v1")])
    library, zotero = stores(tmp_path)
    with ZoteroService.__call__ if False else _client(service) as client:
        preview = plan(zotero, library.list_papers(), connection(), client=client)
    assert preview["applied"] is False
    assert preview["counts"] == {"added": 2, "updated": 0, "linked_existing": 0, "unchanged": 0,
                                 "remote_deleted": 0, "skipped": 0, "unaccounted": 0,
                                 "remote_changes": 2}
    assert preview["cursor"] == {"from": 0, "to": 300}
    # Preview writes nothing at all.
    assert library.list_papers() == [] and zotero.links() == []
    assert zotero.cursor(connection())["committed_version"] == 0

    applied = commit(zotero, preview, connection())
    assert applied["applied"] is True
    papers = library.list_papers()
    assert len(papers) == 2
    by_title = {paper["title"]: paper for paper in papers}
    assert by_title["Layered Canvas Decomposition"]["doi"] == "10.1000/a"
    assert by_title["Constraint-first Layout Generation"]["arxiv_id"] == "2401.00002v1"
    assert len(zotero.links()) == 2
    assert zotero.cursor(connection())["committed_version"] == 300
    assert all(link["state"] == "linked" for link in zotero.links())


def test_a_sync_with_no_changes_writes_nothing_and_duplicates_nothing(tmp_path):
    service = ZoteroService(items=[item("AAAA0001", 300, "Layered Canvas Decomposition",
                                        doi="10.1000/a")],
                            library_version=300)
    library, zotero = stores(tmp_path)
    with _client(service) as client:
        first = plan(zotero, library.list_papers(), connection(), client=client)
    commit(zotero, first, connection())
    assert len(library.list_papers()) == 1

    # Same remote state, same cursor: nothing has changed, so nothing may be created again.
    with _client(service) as client:
        second = plan(zotero, library.list_papers(), connection(), client=client)
    assert second["cursor"] == {"from": 300, "to": 300}
    assert second["counts"]["added"] == 0 and second["counts"]["updated"] == 0
    assert second["counts"]["unchanged"] == 1
    commit(zotero, second, connection())
    assert len(library.list_papers()) == 1
    assert len(zotero.links()) == 1
    # And a third time, because idempotence that holds twice by luck is not idempotence.
    with _client(service) as client:
        third = plan(zotero, library.list_papers(), connection(), client=client)
    commit(zotero, third, connection())
    assert len(library.list_papers()) == 1 and len(zotero.links()) == 1


def test_a_remote_bump_updates_the_bibliography_and_leaves_the_readers_words_alone(tmp_path):
    library, zotero = stores(tmp_path)
    with _client(ZoteroService(items=[item("AAAA0001", 100, "旧标题", doi="10.1000/a")],
                               library_version=100)) as client:
        commit(zotero, plan(zotero, library.list_papers(), connection(), client=client),
               connection())
    paper = library.list_papers()[0]
    edited = library.update_paper(paper["id"], PaperInput(**{
        **{key: paper[key] for key in ("title", "authors", "year", "venue", "abstract", "doi",
                                       "arxiv_id", "paper_url", "topics", "status",
                                       "version_label")},
        "notes": "我的人工批注，不能被远端覆盖", "topics": ["我的分类"], "status": "reading"}))
    assert edited["notes"] == "我的人工批注，不能被远端覆盖"

    with _client(ZoteroService(items=[item("AAAA0001", 250, "远端改了标题", doi="10.1000/a",
                                           abstract="新的摘要")],
                               library_version=250)) as client:
        result = plan(zotero, library.list_papers(), connection(), client=client)
    assert result["counts"]["updated"] == 1 and result["counts"]["added"] == 0
    commit(zotero, result, connection())
    after = library.list_papers()[0]
    assert after["title"] == "远端改了标题" and after["abstract"] == "新的摘要"
    assert after["notes"] == "我的人工批注，不能被远端覆盖"
    assert after["topics"] == ["我的分类"] and after["status"] == "reading"
    assert zotero.link(connection(), "AAAA0001")["remote_version"] == 250


def test_a_remote_deletion_is_a_tombstone_not_a_cascade(tmp_path):
    library, zotero = stores(tmp_path)
    with _client(ZoteroService(items=[item("AAAA0001", 100, "将被远端删除的论文", doi="10.1000/a")],
                               library_version=100)) as client:
        commit(zotero, plan(zotero, library.list_papers(), connection(), client=client),
               connection())
    paper = library.list_papers()[0]
    from re0.models import ResourceInput
    resource = library.create_resource(paper["id"], ResourceInput(kind="code", label="repo",
                                                                  url="https://github.com/a/b"))
    library.save_observation(resource["id"], {"status": "metadata_accessible", "summary": "检查过"})
    library.update_paper(paper["id"], PaperInput(**{
        **{key: paper[key] for key in ("title", "authors", "year", "venue", "abstract", "doi",
                                       "arxiv_id", "paper_url", "topics", "status",
                                       "version_label")},
        "notes": "这条笔记比远端记录重要"}))

    service = ZoteroService(items=[], versions={}, deleted=["AAAA0001"], library_version=180)
    with _client(service) as client:
        result = plan(zotero, library.list_papers(), connection(), client=client)
    assert result["counts"]["remote_deleted"] == 1
    assert "tombstone" in result["remote_deleted"][0]["reason"]
    commit(zotero, result, connection())

    kept = library.get_paper(paper["id"])
    assert kept["notes"] == "这条笔记比远端记录重要"
    assert len(kept["resources"]) == 1
    assert library.history(resource["id"])[0]["status"] == "metadata_accessible"
    assert zotero.link(connection(), "AAAA0001")["state"] == "remote_deleted"
    assert zotero.cursor(connection())["committed_version"] == 180


def test_a_deleted_item_that_comes_back_is_relinked_rather_than_duplicated(tmp_path):
    library, zotero = stores(tmp_path)
    with _client(ZoteroService(items=[item("AAAA0001", 100, "复活的论文", doi="10.1000/a")],
                               library_version=100)) as client:
        commit(zotero, plan(zotero, library.list_papers(), connection(), client=client),
               connection())
    with _client(ZoteroService(items=[], versions={}, deleted=["AAAA0001"],
                               library_version=120)) as client:
        commit(zotero, plan(zotero, library.list_papers(), connection(), client=client),
               connection())
    assert zotero.link(connection(), "AAAA0001")["state"] == "remote_deleted"
    with _client(ZoteroService(items=[item("AAAA0001", 140, "复活的论文", doi="10.1000/a")],
                               library_version=140)) as client:
        result = plan(zotero, library.list_papers(), connection(), client=client)
    assert result["counts"]["added"] == 0 and result["counts"]["updated"] == 1
    assert result["updated"][0]["was_tombstoned"] is True
    commit(zotero, result, connection())
    assert len(library.list_papers()) == 1
    assert zotero.link(connection(), "AAAA0001")["state"] == "linked"


def test_deleting_a_remote_mapping_that_was_never_linked_is_skipped_not_an_error(tmp_path):
    library, zotero = stores(tmp_path)
    with _client(ZoteroService(items=[], versions={}, deleted=["NOSUCH01"],
                               library_version=50)) as client:
        result = plan(zotero, library.list_papers(), connection(), client=client)
    assert result["counts"]["remote_deleted"] == 0
    assert any("没有对应映射" in row["reason"] for row in result["skipped"])


# ----------------------------------------------------------------------------- identity and dedup

def test_the_same_work_in_two_libraries_keeps_two_mappings_and_one_paper(tmp_path):
    library, zotero = stores(tmp_path)
    mine = connection()
    group = Connection(library_type="group", library_id="777", api_key=KEY, label="群组库")
    shared = item("CCCC0003", 90, "两个库都有的同一篇", doi="10.1000/shared")
    with _client(ZoteroService(items=[shared], library_version=90)) as client:
        commit(zotero, plan(zotero, library.list_papers(), mine, client=client), mine)
    group_service = ZoteroService(items=[shared], library_version=90, prefix=GROUP_PREFIX)
    with _client(group_service, group) as client:
        result = plan(zotero, library.list_papers(), group, client=client)
    assert result["counts"]["linked_existing"] == 1 and result["counts"]["added"] == 0
    assert result["linked_existing"][0]["matched_on"] == "doi"
    commit(zotero, result, group)
    assert len(library.list_papers()) == 1
    links = zotero.links()
    assert len(links) == 2
    assert {(link["library_type"], link["library_id"]) for link in links} == {("user", "12345"),
                                                                             ("group", "777")}
    assert len({link["paper_id"] for link in links}) == 1




def test_an_item_that_matches_an_existing_paper_is_linked_without_rewriting_it(tmp_path):
    library, zotero = stores(tmp_path)
    existing = library.create_paper(PaperInput(title="已经用 CSL 导入过的论文",
                                               doi="10.1000/csl", notes="CSL 导入时写的笔记"))
    with _client(ZoteroService(items=[item("DDDD0004", 60, "已经用 CSL 导入过的论文",
                                           doi="10.1000/csl", abstract="远端的摘要")],
                               library_version=60)) as client:
        result = plan(zotero, library.list_papers(), connection(), client=client)
    assert result["counts"]["added"] == 0 and result["counts"]["linked_existing"] == 1
    commit(zotero, result, connection())
    assert len(library.list_papers()) == 1
    after = library.get_paper(existing["id"])
    # Linking is not importing: the record the reader already had is untouched.
    assert after["notes"] == "CSL 导入时写的笔记"
    assert after["abstract"] == ""
    assert zotero.link(connection(), "DDDD0004")["paper_id"] == existing["id"]


def test_a_remote_doi_edit_that_collides_with_another_paper_touches_neither_side(tmp_path):
    library, zotero = stores(tmp_path)
    with _client(ZoteroService(items=[item("EEEE0005", 100, "第一篇论文", doi="10.1000/mine")],
                               library_version=100)) as client:
        commit(zotero, plan(zotero, library.list_papers(), connection(), client=client),
               connection())
    library.create_paper(PaperInput(title="另一篇完全无关的论文", doi="10.1000/theirs"))
    # The remote item's DOI is edited to one a different local paper already holds. Writing it would
    # break the unique index and roll the whole transaction back, so neither record is touched.
    with _client(ZoteroService(items=[item("EEEE0005", 200, "第一篇论文", doi="10.1000/theirs")],
                               library_version=200)) as client:
        result = plan(zotero, library.list_papers(), connection(), client=client)
    assert result["counts"]["updated"] == 0 and result["counts"]["added"] == 0
    assert any("已属于库内另一条记录" in row.get("reason", "") for row in result["skipped"])
    commit(zotero, result, connection())
    assert {paper["doi"]: paper["title"] for paper in library.list_papers()} == {
        "10.1000/mine": "第一篇论文", "10.1000/theirs": "另一篇完全无关的论文"}
    # The mapping keeps the version it last wrote, so the skip is visible rather than silently applied.
    assert zotero.link(connection(), "EEEE0005")["remote_version"] == 100
    assert zotero.cursor(connection())["committed_version"] == 200
    assert any("游标仍会前进" in note for note in result["notes"])
    assert result["skipped"][:1] == zotero.status(connection())["recent_syncs"][0]["skipped"][:1]


def test_two_remote_items_sharing_one_doi_do_not_abort_the_whole_sync(tmp_path):
    """One duplicate in the remote library must not roll back every other change in the window."""
    library, zotero = stores(tmp_path)
    items = [item("IIII0009", 20, "共享 DOI 的第一条", doi="10.1000/same"),
             item("JJJJ0010", 20, "共享 DOI 的第二条", doi="10.1000/same"),
             item("KKKK0011", 20, "毫不相干的第三条", doi="10.1000/other")]
    with _client(ZoteroService(items=items, library_version=20)) as client:
        result = plan(zotero, library.list_papers(), connection(), client=client)
    assert result["counts"]["added"] == 2
    assert any("已占用 DOI" in row.get("reason", "") for row in result["skipped"])
    commit(zotero, result, connection())
    assert len(library.list_papers()) == 2
    assert len(zotero.links()) == 2


def test_two_library_records_with_the_same_title_are_not_guesses_about_each_other(tmp_path):
    library, zotero = stores(tmp_path)
    library.create_paper(PaperInput(title="重名的第一篇论文标题需要足够长才能被归一化匹配", doi="10.1000/one"))
    library.create_paper(PaperInput(title="重名的第一篇论文标题需要足够长才能被归一化匹配", doi="10.1000/two"))
    with _client(ZoteroService(items=[item("FFFF0006", 80, "重名的第一篇论文标题需要足够长才能被归一化匹配")],
                               library_version=80)) as client:
        result = plan(zotero, library.list_papers(), connection(), client=client)
    assert result["counts"]["added"] == 0 and result["counts"]["linked_existing"] == 0
    assert any("无法确定对应哪一条" in row.get("reason", "") for row in result["skipped"])


def test_two_items_in_one_sync_sharing_a_title_do_not_become_one_paper(tmp_path):
    library, zotero = stores(tmp_path)
    items = [item("GGGG0007", 10, "同一个标题的两条远端记录"),
             item("HHHH0008", 10, "同一个标题的两条远端记录")]
    with _client(ZoteroService(items=items, library_version=10)) as client:
        result = plan(zotero, library.list_papers(), connection(), client=client)
    assert result["counts"]["added"] == 2
    commit(zotero, result, connection())
    assert len(library.list_papers()) == 2
    assert len(zotero.links()) == 2


# ------------------------------------------------------------------------------ scope and honesty

def test_changes_the_request_did_not_ask_for_are_counted_not_silently_dropped(tmp_path):
    library, zotero = stores(tmp_path)
    items = [item("AAAA0001", 300, "唯一读取到的文献条目", doi="10.1000/a")]
    versions = {"AAAA0001": 300, "NOTE0001": 300, "ATTACH01": 300, "ANN00001": 300}
    with _client(ZoteroService(items=items, versions=versions, total=1,
                               library_version=300)) as client:
        result = plan(zotero, library.list_papers(), connection(), client=client)
    assert result["counts"]["remote_changes"] == 4
    assert result["counts"]["added"] == 1
    assert result["counts"]["unaccounted"] == 3
    assert any("不在本次读取范围内" in note for note in result["notes"])


def test_the_item_request_names_an_inclusion_list_that_excludes_private_types(tmp_path):
    service = ZoteroService(items=[item("AAAA0001", 300, "标题", doi="10.1000/a")],
                            library_version=300)
    library, zotero = stores(tmp_path)
    with _client(service) as client:
        plan(zotero, library.list_papers(), connection(), client=client)
    listing = [request for request in service.requests
               if request.url.path == PREFIX + "/items"
               and request.url.params.get("format") == "json"]
    assert listing, "no item listing was requested"
    asked = listing[0].url.params.get("itemType", "")
    assert "journalArticle" in asked
    for private in ("attachment", "note", "annotation"):
        assert private not in asked.split("||")


def test_an_incomplete_page_read_refuses_to_sync_and_leaves_the_cursor_alone(tmp_path):
    library, zotero = stores(tmp_path)
    # Total-Results promises two items but the page returns none: the read stopped early.
    service = ZoteroService(items=[], total=2, library_version=300, short_page=True)
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as caught:
        sync(zotero, library.list_papers(), connection(),
             transport=httpx.MockTransport(service), apply=True)
    assert caught.value.status_code == 502
    assert "游标未移动" in str(caught.value.detail)
    assert zotero.cursor(connection())["committed_version"] == 0
    assert library.list_papers() == []


def test_a_stale_plan_is_refused_instead_of_being_applied_on_top_of_a_newer_one(tmp_path):
    library, zotero = stores(tmp_path)
    service = ZoteroService(items=[item("AAAA0001", 300, "第一篇", doi="10.1000/a")],
                            library_version=300)
    with _client(service) as client:
        stale = plan(zotero, library.list_papers(), connection(), client=client)
    with _client(ZoteroService(items=[item("BBBB0002", 400, "第二篇", doi="10.1000/b")],
                               library_version=400)) as client:
        fresh = plan(zotero, library.list_papers(), connection(), client=client)
    commit(zotero, fresh, connection())
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as caught:
        commit(zotero, stale, connection())
    assert caught.value.status_code == 409
    assert "已过期" in str(caught.value.detail)
    assert len(library.list_papers()) == 1


def test_the_sync_entry_point_previews_by_default_and_applies_only_when_asked(tmp_path):
    library, zotero = stores(tmp_path)
    service = ZoteroService(items=[item("AAAA0001", 300, "通过 sync() 进来的条目",
                                        doi="10.1000/a")], library_version=300)
    preview = sync(zotero, library.list_papers(), connection(),
                   transport=httpx.MockTransport(service), apply=False)
    assert preview["applied"] is False and preview["counts"]["added"] == 1
    assert library.list_papers() == [] and zotero.links() == []
    assert "没有写入任何内容" in preview["note"]

    applied = sync(zotero, library.list_papers(), connection(),
                   transport=httpx.MockTransport(service), apply=True)
    assert applied["applied"] is True
    assert len(library.list_papers()) == 1
    assert zotero.cursor(connection())["committed_version"] == 300
    assert applied["requests_used"] >= 3


# ------------------------------------------------------------------------------------ credentials

def test_the_key_is_never_stored_logged_or_returned(tmp_path):
    library, zotero = stores(tmp_path)
    service = ZoteroService(items=[item("AAAA0001", 300, "标题", doi="10.1000/a")],
                            library_version=300)
    result = sync(zotero, library.list_papers(), connection(),
                  transport=httpx.MockTransport(service), apply=True)
    assert KEY not in json.dumps(result, ensure_ascii=False)
    assert result["connection"]["has_api_key"] is True
    with library.db.connect() as con:
        dump = "\n".join(con.iterdump())
    assert KEY not in dump
    assert "zotero_links" in dump and "zotero_cursors" in dump
    status = zotero.status(connection())
    assert KEY not in json.dumps(status, ensure_ascii=False)
    assert status["links"]["linked"] == 1 and status["distinct_papers"] == 1
    assert status["recent_syncs"][0]["counts"]["added"] == 1


def test_a_connection_without_a_key_is_refused_before_any_request(tmp_path):
    from re0.zotero import ZoteroClient
    with pytest.raises(ZoteroError):
        ZoteroClient(Connection(library_type="user", library_id="12345", api_key=""))


# ----------------------------------------------------------------------------------- disconnection

def test_disconnecting_forgets_the_mapping_and_keeps_the_library(tmp_path):
    library, zotero = stores(tmp_path)
    with _client(ZoteroService(items=[item("AAAA0001", 300, "断开连接后仍应留下的论文",
                                           doi="10.1000/a")], library_version=300)) as client:
        commit(zotero, plan(zotero, library.list_papers(), connection(), client=client),
               connection())
    paper = library.list_papers()[0]
    library.update_paper(paper["id"], PaperInput(**{
        **{key: paper[key] for key in ("title", "authors", "year", "venue", "abstract", "doi",
                                       "arxiv_id", "paper_url", "topics", "status",
                                       "version_label")},
        "notes": "断开连接也不能删掉这条"}))
    result = zotero.disconnect(connection())
    assert result["disconnected"] is True and result["removed_links"] == 0
    assert zotero.links() and zotero.links()[0]["state"] == "linked"
    assert zotero.cursor(connection())["committed_version"] == 0

    full = zotero.disconnect(connection(), remove_links=True)
    assert full["removed_links"] == 1
    assert zotero.links() == []
    kept = library.get_paper(paper["id"])
    assert kept["notes"] == "断开连接也不能删掉这条"
    assert full["papers_remaining"] == 1


def test_deleting_a_paper_removes_its_mapping_but_not_the_remote_record(tmp_path):
    library, zotero = stores(tmp_path)
    with _client(ZoteroService(items=[item("AAAA0001", 300, "将被本地删除的论文", doi="10.1000/a")],
                               library_version=300)) as client:
        commit(zotero, plan(zotero, library.list_papers(), connection(), client=client),
               connection())
    paper = library.list_papers()[0]
    library.delete_paper(paper["id"])
    assert zotero.links() == []
    assert library.list_papers() == []
