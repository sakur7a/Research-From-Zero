"""Continuing a conversation: follow-ups, retries, the cumulative ledger, and the v1 -> v2 migration.

Two layers are tested separately on purpose. The service layer (`re0.agent.session`) is exercised
against a task store with no model and no network at all, because scope validation is the part that
must be right whether or not anybody ever runs a turn. The end-to-end tests then drive a fixture
model through two real turns to prove the reuse actually reaches the report.
"""
import json
import re
import time
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from fastapi.testclient import TestClient

from re0.agent.schemas import FollowUpInput, RetryInput, SessionCaps, TaskDefaults
from re0.agent.session import (STALE_AFTER_DAYS, Scope, age_days, handover_message,
                               parent_report_text, report_delta, turn_snapshot, validate_followup,
                               validate_retry)
from re0.agent.storage import TaskStore, default_caps
from re0.db import Database, encode
from re0.main import create_app
from re0.workspace import Workspace

CONFIG = {"base_url": "https://api.openai.com/v1", "model": "fixture-model",
          "api_key": "sk-test-do-not-persist", "trust_endpoint": True}
DESTINATION = {"base_url": CONFIG["base_url"], "model": CONFIG["model"],
               "token_parameter": "max_tokens", "max_output_tokens": 3000}
OTHER_DESTINATION = {**DESTINATION, "model": "a-different-model"}
GOAL = {"goal": "查找 layout 论文及资源，保留证据", "consent_to_send": True}
HEADERS = {"X-Re0-Client": "web", "Content-Type": "application/json"}
CLI_HEADERS = {"X-Re0-Client": "cli", "Content-Type": "application/json"}
ATOM = b'''<feed xmlns="http://www.w3.org/2005/Atom"><entry><id>http://arxiv.org/abs/2501.12345v2</id><title>Fixture Layout Paper</title><summary>TEST FIXTURE: a layout study. This is not a real paper claim.</summary><published>2025-01-21T00:00:00Z</published><author><name>Test Author</name></author></entry></feed>'''
EVIDENCE_ID = re.compile(r"ev_[0-9a-f]{16}")


def _completion(name, args):
    return {"choices": [{"finish_reason": "tool_calls",
                         "message": {"role": "assistant", "content": "", "tool_calls": [
                             {"id": "call_fixture", "type": "function",
                              "function": {"name": name, "arguments": json.dumps(args, ensure_ascii=False)}}]}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}}


def _report(ids, marker="首轮"):
    findings = [{"claim": f"{marker}：检索到了摘要；尚未验证训练代码或权重。",
                 "evidence_ids": ids[:8], "assessment": "uncertain"}] if ids else []
    return {"title": f"Fixture report {marker}", "outcome": "findings" if findings else "insufficient_evidence",
            "summary": f"基于 fixture 来源整理的测试报告（{marker}）", "findings": findings,
            "limitations": ["这是离线测试，不是真实模型与外网效果评测。"]}


def _check_protocol(messages):
    pending = set()
    for message in messages:
        if message["role"] == "assistant":
            assert not pending, "Model called before all tool responses arrived"
            pending.update(x["id"] for x in message.get("tool_calls", []))
        elif message["role"] == "tool":
            assert message["tool_call_id"] in pending, "Tool response lost its assistant call"
            pending.remove(message["tool_call_id"])
        else:
            assert not pending, "Non-tool message before pending tools were answered"
    assert not pending


class TurnModel:
    """A fixture model that plans, searches on a first turn, and reports over visible evidence ids.

    It finds the ids by reading them out of the conversation, which is the only honest way to model a
    continuing turn: the reused material arrives in the handover message, not in a tool result.
    """

    def __init__(self):
        self.calls = 0
        self.arxiv_hits = 0
        self.reports = []

    def __call__(self, request):
        if request.url.host == "export.arxiv.org":
            self.arxiv_hits += 1
            return httpx.Response(200, content=ATOM)
        data = json.loads(request.content)
        if isinstance(data.get("tool_choice"), dict):
            return httpx.Response(200, json=_completion("connection_check", {}))
        _check_protocol(data["messages"])
        self.calls += 1
        text = json.dumps(data["messages"], ensure_ascii=False)
        continuing = "本轮是第" in text
        tools = sum(1 for m in data["messages"] if m["role"] == "tool")
        if tools == 0:
            return httpx.Response(200, json=_completion(
                "update_plan", {"steps": ["复用已授权的证据", "必要时补查", "给出本轮报告"]}))
        if tools == 1 and not continuing:
            return httpx.Response(200, json=_completion(
                "search_papers", {"query": "layout", "limit": 1, "source": "arxiv"}))
        ids = sorted(set(EVIDENCE_ID.findall(text)))
        report = _report(ids, "追问轮" if continuing else "首轮")
        self.reports.append(report)
        return httpx.Response(200, json=_completion("finish_report", report))


def _wait(client, rid):
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        run = client.get("/api/agent/runs/" + rid).json()
        if run["status"] not in {"queued", "running"} and not client.get("/api/agent/config").json()["busy"]:
            return run
        time.sleep(.015)
    raise AssertionError("Fixture task did not finish")


def _first_turn(client, goal=None):
    run = client.post("/api/agent/runs", json={**GOAL, **(goal or {})}).json()
    final = _wait(client, run["id"])
    assert final["status"] == "completed", final
    return final


def _followup(client, parent, ids, **extra):
    body = {"parent_run": parent, "goal": "只保留有训练代码的两篇，并补查它们的数据划分",
            "reuse_evidence": ids, "authorize_spend": True, "consent_to_send": True, **extra}
    return client.post("/api/agent/followups", json=body)


# --------------------------------------------------------------------------- pure service functions

def test_age_of_unknown_material_is_not_reported_as_fresh():
    old = (datetime.now(timezone.utc) - timedelta(days=STALE_AFTER_DAYS + 1)).isoformat()
    assert age_days(old) > STALE_AFTER_DAYS
    # An unparseable or missing stamp is None, never 0: "we could not tell" is not "just retrieved".
    assert age_days("") is None and age_days("not a time") is None and age_days(None) is None
    naive = (datetime.now() - timedelta(days=2)).isoformat()
    assert 1 < age_days(naive) < 3


def test_report_delta_matches_a_reworded_finding_instead_of_calling_it_new():
    previous = {"run_id": "r1", "turn": 1,
                "report": _report(["ev_a"], "首轮")}
    current = _report(["ev_a"], "追问轮")
    delta = report_delta(previous, current)
    assert delta["added"] == [] and delta["dropped"] == []
    assert len(delta["changed"]) == 1
    assert delta["changed"][0]["from"]["claim"].startswith("首轮")
    assert delta["changed"][0]["to"]["claim"].startswith("追问轮")
    assert delta["outcome"] == {"previous": "findings", "current": "findings"}
    assert len(delta["still_uncertain"]) == 1


def test_report_delta_maps_reused_ids_back_so_a_carried_finding_is_not_churn():
    """Reuse mints a new evidence id per turn; without the map this reads as one add and one drop."""
    previous = {"run_id": "r1", "turn": 1, "report": _report(["ev_origin"], "首轮")}
    current = _report(["ev_reused"], "追问轮")
    unmapped = report_delta(previous, current)
    assert len(unmapped["added"]) == 1 and len(unmapped["dropped"]) == 1
    mapped = report_delta(previous, current, id_map={"ev_reused": "ev_origin"})
    assert mapped["added"] == [] and mapped["dropped"] == []
    assert len(mapped["changed"]) == 1


def test_report_delta_dropped_is_not_a_retraction_and_says_so():
    previous = {"run_id": "r1", "turn": 1, "report": _report(["ev_a"], "首轮")}
    delta = report_delta(previous, _report(["ev_b"], "追问轮"))
    assert len(delta["added"]) == 1 and len(delta["dropped"]) == 1
    assert "不表示上一轮结论被推翻" in delta["note"]
    assert delta["against_run"] == "r1" and delta["against_turn"] == 1


def test_report_delta_resolves_uncertainty_and_tracks_what_is_still_open():
    previous = {"run_id": "r1", "turn": 1, "report": {
        **_report(["ev_a"]), "findings": [{"claim": "有训练脚本候选", "evidence_ids": ["ev_a"],
                                          "assessment": "uncertain"}]}}
    current = {**_report(["ev_a"]), "findings": [
        {"claim": "训练脚本确认存在", "evidence_ids": ["ev_a"], "assessment": "observed"},
        {"claim": "数据划分仍未确认", "evidence_ids": ["ev_b"], "assessment": "uncertain"}]}
    delta = report_delta(previous, current)
    assert len(delta["resolved_from_uncertain"]) == 1
    assert delta["resolved_from_uncertain"][0]["to"]["assessment"] == "observed"
    assert [x["claim"] for x in delta["still_uncertain"]] == ["数据划分仍未确认"]
    assert len(delta["added"]) == 1


def test_first_turn_has_nothing_to_compare_and_says_that():
    delta = report_delta(None, _report(["ev_a"]))
    assert delta["against_run"] == "" and delta["against_turn"] == 0
    assert len(delta["added"]) == 1 and delta["changed"] == [] and delta["dropped"] == []
    assert "此前没有已完成的报告" in delta["note"]


def test_the_handover_never_shows_evidence_ids_the_new_turn_cannot_cite():
    """The parent's ids belong to the parent's run; a report citing one is rejected."""
    previous = _report(["ev_aaaaaaaaaaaaaaaa", "ev_bbbbbbbbbbbbbbbb"])
    text = parent_report_text(previous)
    assert "ev_aaaaaaaaaaaaaaaa" not in text
    assert "不能直接引用" in text
    assert parent_report_text(None).startswith("上一轮没有产出结构化报告")


def test_the_handover_is_bounded_however_much_history_exists():
    huge = {"title": "x" * 400, "outcome": "findings", "summary": "y" * 20000,
            "findings": [{"claim": "z" * 1800, "evidence_ids": ["ev_a"], "assessment": "uncertain"}] * 20,
            "limitations": ["w" * 1000] * 12}
    assert len(parent_report_text(huge)) <= 4000


def test_reuse_excerpts_share_one_budget_rather_than_growing_with_the_list():
    scope = Scope(conversation_id="cv", parent_run="r1", parent_turn=1, turn=2, kind="followup",
                  goal="g", reuse=[])
    for index in range(40):
        scope.reuse.append(_reuse(f"ev_{index:04d}", "q" * 4000))
    text = handover_message(scope, parent_goal="earlier", parent_report=None)
    # 40 items x 4000 chars would be 160k; the excerpt budget is shared, so the total stays bounded.
    assert len(text) < 20000
    assert text.count("已截断") > 0


def _reuse(eid, body):
    from re0.agent.session import Reuse
    return Reuse(evidence_id=eid, origin={"route": "conversation", "run_id": "r1", "turn": 1,
                                          "evidence_id": eid},
                 retrieved_at=datetime.now(timezone.utc).isoformat(), age=0.0, stale=False,
                 summary="《Fixture》", excerpt=body, kind="paper", tool="search_papers")


def test_turn_snapshot_records_every_authorization_the_turn_rests_on():
    scope = Scope(conversation_id="cv", parent_run="r1", parent_turn=1, turn=2, kind="followup",
                  goal="只保留有训练代码的两篇", reuse=[_reuse("ev_1", "body")],
                  authorizations={"use_library": True, "spend": True, "reuse_count": 1},
                  caps=default_caps(), ledger={"model_calls": 3, "tool_calls": 3, "turns": 1,
                                               "unreported_calls": 0})
    snapshot = turn_snapshot(scope, config_public=DESTINATION, budgets=TaskDefaults(),
                             allowed_tools=["search_papers", "finish_report"])
    assert snapshot["goal"] == "只保留有训练代码的两篇"
    assert snapshot["model_destination"]["model"] == "fixture-model"
    assert "api_key" not in json.dumps(snapshot)
    assert snapshot["permissions"]["use_library"] is True
    assert snapshot["permissions"]["allowed_tools"] == ["search_papers", "finish_report"]
    assert snapshot["ledger_before_turn"]["model_calls"] == 3
    assert snapshot["reuse"][0]["evidence_id"] == "ev_1"
    assert snapshot["authorized_at"]


# --------------------------------------------------------------------------- store level, no model

def _store(tmp_path) -> TaskStore:
    return TaskStore(Database(str(tmp_path / "session.sqlite3")))


def _run(store, *, goal="第一轮目标", status="completed", conversation=None, evidence=(),
         config=None, params=None, turn=None, kind="new"):
    conversation = conversation or store.start_conversation(goal, default_caps())
    state = {"messages": [], "pending": [], "plan": [], "report": None, "model_calls": 3,
             "tool_calls": 3, "usage": {"prompt_tokens": 0, "completion_tokens": 0,
                                        "total_tokens": 0, "unreported_calls": 0},
             "resumes": 0, "reserved_tools": []}
    rid = store.create({"goal": goal, "consent_to_send": True,
                        **(params or TaskDefaults().model_dump())},
                       config or DESTINATION, state, conversation_id=conversation,
                       turn=turn or store.next_turn(conversation), kind=kind)
    if evidence:
        store.seed_evidence(rid, [{"data": item, "reused_from": {}} for item in evidence])
    store.checkpoint(rid, state, status)
    return rid, conversation


def _follow(**extra):
    body = {"goal": "只保留有训练代码的两篇", "authorize_spend": True, "consent_to_send": True, **extra}
    return FollowUpInput(**body)


def test_the_ledger_is_recomputed_so_a_new_turn_cannot_reset_it(tmp_path):
    store = _store(tmp_path)
    rid, cid = _run(store)
    assert store.ledger(cid)["model_calls"] == 3
    _run(store, conversation=cid, turn=2, kind="followup")
    ledger = store.ledger(cid)
    assert ledger["model_calls"] == 6 and ledger["turns"] == 2
    # Nothing in the conversation row is incremented, so there is nothing to zero out.
    assert "ledger" not in store.conversation(cid) or store.conversation(cid)["ledger"] == ledger


def test_seeding_reused_evidence_twice_does_not_duplicate_it(tmp_path):
    store = _store(tmp_path)
    rid, _ = _run(store)
    rows = [{"data": {"kind": "paper", "paper": {"title": "T"}, "retrieved_at": "2026-01-01T00:00:00",
                      "reused_from": {"route": "conversation", "run_id": rid, "turn": 1,
                                      "evidence_id": "ev_origin"}},
             "reused_from": {"route": "conversation", "run_id": rid, "turn": 1,
                             "evidence_id": "ev_origin"}}]
    first = store.seed_evidence(rid, rows)
    second = store.seed_evidence(rid, rows)
    assert first == second
    assert len([x for x in store.evidence(rid) if x["id"] == first[0]]) == 1


def test_the_origin_snapshot_survives_every_later_checkpoint(tmp_path):
    store = _store(tmp_path)
    rid, _ = _run(store)
    snapshot = turn_snapshot(Scope(conversation_id="cv", parent_run="", parent_turn=0, turn=1,
                                   kind="new", goal="第一轮目标"),
                             config_public=DESTINATION, budgets=TaskDefaults(), allowed_tools=["a"])
    with store.db.connect() as con:
        con.execute("UPDATE agent_runs SET origin=? WHERE id=?", (encode(snapshot), rid))
    before = store.origin(rid)
    state = store.get(rid, internal=True)["state"]
    for status in ("running", "completed", "budget_exhausted"):
        store.checkpoint(rid, state, status)
        assert store.origin(rid) == before, f"checkpoint at {status} rewrote the snapshot"


def test_a_run_from_a_v1_database_keeps_its_data_and_gains_a_conversation(tmp_path):
    path = str(tmp_path / "legacy.sqlite3")
    Database(path)
    v1 = """
    CREATE TABLE agent_schema_version (version INTEGER NOT NULL);
    INSERT INTO agent_schema_version VALUES (1);
    CREATE TABLE agent_runs (
     id TEXT PRIMARY KEY, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
     status TEXT NOT NULL, goal TEXT NOT NULL, config TEXT NOT NULL, params TEXT NOT NULL,
     state TEXT NOT NULL, error TEXT NOT NULL DEFAULT '', cancel_requested INTEGER NOT NULL DEFAULT 0);
    CREATE TABLE agent_events (id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL,
     at TEXT NOT NULL, kind TEXT NOT NULL, data TEXT NOT NULL);
    CREATE TABLE agent_evidence (id TEXT PRIMARY KEY, run_id TEXT NOT NULL,
     tool_call_id TEXT NOT NULL, data TEXT NOT NULL);
    CREATE TABLE agent_tool_results (run_id TEXT NOT NULL, call_id TEXT NOT NULL, data TEXT NOT NULL,
     PRIMARY KEY(run_id, call_id));
    CREATE TABLE agent_imports (run_id TEXT NOT NULL, evidence_id TEXT NOT NULL,
     paper_id TEXT NOT NULL, at TEXT NOT NULL, PRIMARY KEY(run_id, evidence_id));
    CREATE TABLE agent_settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
    """
    state = {"messages": [{"role": "user", "content": "旧任务"}], "pending": [], "plan": [],
             "report": _report(["ev_legacy0000000000"]), "model_calls": 7, "tool_calls": 9,
             "usage": {"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3,
                       "unreported_calls": 1},
             "resumes": 1, "reserved_tools": []}
    db = Database(path)
    with db.connect() as con:
        con.executescript(v1)
        con.execute("INSERT INTO agent_runs(id,created_at,updated_at,status,goal,config,params,state) "
                    "VALUES ('legacy-run','2026-01-01T00:00:00','2026-01-01T01:00:00','completed',"
                    "'旧的已完成任务',?,?,?)",
                    (encode(DESTINATION), encode({"goal": "旧的已完成任务", "use_library": True}),
                     encode(state)))
        con.execute("INSERT INTO agent_evidence VALUES ('ev_legacy0000000000','legacy-run','c1',?)",
                    (encode({"kind": "paper", "paper": {"title": "旧证据"}}),))
        con.execute("INSERT INTO agent_settings VALUES ('task_defaults',?)",
                    (encode({"max_model_calls": 9}),))

    store = TaskStore(Database(path))
    run = store.get("legacy-run", internal=True)
    assert run["goal"] == "旧的已完成任务" and run["status"] == "completed"
    assert run["turn"] == 1 and run["kind"] == "new"
    cid = run["conversation_id"]
    assert cid and store.conversation(cid)["note"].startswith("由 v1 迁移生成")
    # The checkpoint counters became the opening ledger: a migration must not erase what was spent.
    ledger = store.ledger(cid)
    assert ledger["model_calls"] == 7 and ledger["tool_calls"] == 9
    assert ledger["unreported_calls"] == 1 and ledger["turns"] == 1
    assert store.evidence("legacy-run")[0]["paper"]["title"] == "旧证据"
    assert store.setting("task_defaults") == {"max_model_calls": 9}
    assert any(event["kind"] == "conversation_adopted"
               for event in store.events("legacy-run"))
    # Re-opening is a no-op rather than a second adoption.
    again = TaskStore(Database(path))
    assert again.get("legacy-run", internal=True)["conversation_id"] == cid
    assert len(again.conversations()) == 1
    with Database(path).connect() as con:
        assert con.execute("SELECT version FROM agent_schema_version").fetchone()[0] == 2


def test_a_database_from_a_later_version_is_refused_rather_than_downgraded(tmp_path):
    path = str(tmp_path / "future.sqlite3")
    db = Database(path)
    TaskStore(db)
    with db.connect() as con:
        con.execute("UPDATE agent_schema_version SET version=99")
    with pytest.raises(RuntimeError, match="no destructive migration"):
        TaskStore(Database(path))


# ----------------------------------------------------------------------- scope validation, no model

def test_reuse_from_another_conversation_is_refused_and_names_the_reason(tmp_path):
    store = _store(tmp_path)
    mine, cid = _run(store, evidence=[{"kind": "paper", "paper": {"title": "A"}}])
    foreign_run, _ = _run(store, goal="另一个会话", evidence=[{"kind": "paper", "paper": {"title": "B"}}])
    foreign = store.evidence(foreign_run)[0]["id"]
    with pytest.raises(Exception) as caught:
        validate_followup(store, _follow(parent_run=mine, reuse_evidence=[foreign]),
                          vault_public=DESTINATION, budgets=TaskDefaults())
    assert caught.value.status_code == 409
    assert "另一个会话" in str(caught.value.detail)
    # Refused, not dropped: nothing was created.
    assert len(store.runs_in(cid)) == 1


def test_an_unknown_evidence_id_is_a_404_not_a_silent_omission(tmp_path):
    store = _store(tmp_path)
    rid, _ = _run(store)
    with pytest.raises(Exception) as caught:
        validate_followup(store, _follow(parent_run=rid, reuse_evidence=["ev_doesnotexist00"]),
                          vault_public=DESTINATION, budgets=TaskDefaults())
    assert caught.value.status_code == 404


def test_a_followup_while_the_parent_is_still_running_is_refused(tmp_path):
    store = _store(tmp_path)
    rid, _ = _run(store, status="running")
    with pytest.raises(Exception) as caught:
        validate_followup(store, _follow(parent_run=rid), vault_public=DESTINATION,
                          budgets=TaskDefaults())
    assert caught.value.status_code == 409
    assert "并发" in str(caught.value.detail)


def test_a_changed_model_destination_needs_explicit_trust(tmp_path):
    store = _store(tmp_path)
    rid, _ = _run(store)
    with pytest.raises(Exception) as caught:
        validate_followup(store, _follow(parent_run=rid), vault_public=OTHER_DESTINATION,
                          budgets=TaskDefaults())
    assert caught.value.status_code == 409
    assert "trust_new_destination" in str(caught.value.detail)
    scope, _ = validate_followup(store, _follow(parent_run=rid, trust_new_destination=True),
                                 vault_public=OTHER_DESTINATION, budgets=TaskDefaults())
    assert any("目的地" in note for note in scope.notes)
    assert scope.authorizations["trust_new_destination"] is True


def test_an_unreadable_destination_is_reported_as_unchecked_not_as_matching(tmp_path):
    store = _store(tmp_path)
    rid, _ = _run(store)
    scope, _ = validate_followup(store, _follow(parent_run=rid), vault_public={},
                                 budgets=TaskDefaults())
    assert any("没有比较" in note for note in scope.notes)


def test_library_consent_is_re_asked_on_a_followup_and_recorded(tmp_path):
    store = _store(tmp_path)
    rid, _ = _run(store, params={**TaskDefaults().model_dump(), "use_library": False})
    scope, _ = validate_followup(store, _follow(parent_run=rid), vault_public=DESTINATION,
                                 budgets=TaskDefaults())
    assert scope.authorizations["use_library"] is False
    assert scope.authorizations["library_reauthorized"] is False
    granted, _ = validate_followup(store, _follow(parent_run=rid, use_library=True),
                                   vault_public=DESTINATION, budgets=TaskDefaults())
    assert granted.authorizations["library_reauthorized"] is True
    assert any("重新给出" in note for note in granted.notes)


def test_a_retry_repeats_the_goal_verbatim_and_inherits_the_library_authorization(tmp_path):
    store = _store(tmp_path)
    rid, _ = _run(store, goal="原始目标原文", status="failed",
                  params={**TaskDefaults().model_dump(), "use_library": True})
    scope, seeds = validate_retry(store, RetryInput(parent_run=rid, authorize_spend=True,
                                                    consent_to_send=True),
                                  vault_public=DESTINATION, budgets=TaskDefaults())
    assert scope.goal == "原始目标原文" and scope.kind == "retry" and seeds == []
    assert scope.authorizations["use_library"] is True
    assert scope.authorizations["library_reauthorized"] is False
    assert any("沿用上一轮的目标原文" in note for note in scope.notes)


def test_a_retry_is_refused_for_a_completed_turn_and_for_a_running_one(tmp_path):
    store = _store(tmp_path)
    done, _ = _run(store, status="completed")
    with pytest.raises(Exception) as caught:
        validate_retry(store, RetryInput(parent_run=done, authorize_spend=True, consent_to_send=True),
                       vault_public=DESTINATION, budgets=TaskDefaults())
    assert caught.value.status_code == 409 and "追问" in str(caught.value.detail)
    live, _ = _run(store, goal="另一个", status="running")
    with pytest.raises(Exception) as caught:
        validate_retry(store, RetryInput(parent_run=live, authorize_spend=True, consent_to_send=True),
                       vault_public=DESTINATION, budgets=TaskDefaults())
    assert "并发" in str(caught.value.detail)


def test_a_spent_session_cap_blocks_a_followup_until_it_is_explicitly_raised(tmp_path):
    store = _store(tmp_path)
    rid, cid = _run(store)
    with store.db.connect() as con:
        con.execute("UPDATE agent_conversations SET caps=? WHERE id=?",
                    (encode({"max_session_model_calls": 3, "max_session_tool_calls": 3}), cid))
    with pytest.raises(Exception) as caught:
        validate_followup(store, _follow(parent_run=rid), vault_public=DESTINATION,
                          budgets=TaskDefaults())
    assert caught.value.status_code == 409
    assert "raise_session_caps" in str(caught.value.detail)
    scope, _ = validate_followup(store, _follow(
        parent_run=rid, raise_session_caps=SessionCaps(max_session_model_calls=20,
                                                       max_session_tool_calls=40)),
        vault_public=DESTINATION, budgets=TaskDefaults())
    assert scope.caps["max_session_model_calls"] == 20
    assert any("上限将被显式提高" in note for note in scope.notes)
    # Validation alone must not move the cap: a later check can still refuse the turn.
    assert store.conversation(cid)["caps"]["max_session_model_calls"] == 3


def test_a_followup_cannot_lower_a_session_cap(tmp_path):
    store = _store(tmp_path)
    rid, cid = _run(store)
    with store.db.connect() as con:
        con.execute("UPDATE agent_conversations SET caps=? WHERE id=?",
                    (encode({"max_session_model_calls": 30, "max_session_tool_calls": 60}), cid))
    with pytest.raises(Exception) as caught:
        validate_followup(store, _follow(parent_run=rid, raise_session_caps=SessionCaps(
            max_session_model_calls=30, max_session_tool_calls=4)),
            vault_public=DESTINATION, budgets=TaskDefaults())
    assert caught.value.status_code == 422


def test_reuse_from_a_workspace_keeps_the_original_retrieval_time(tmp_path):
    store = _store(tmp_path)
    rid, _ = _run(store)
    workspace = Workspace(tmp_path / "ws").open()
    source_id = workspace.record({"source_url": "https://export.arxiv.org/abs/2501.12345",
                                  "locator": "arXiv:2501.12345", "kind": "paper",
                                  "content": "fixture body", "paper": {"title": "Fixture"}},
                                 tool="search_papers")
    scope, seeds = validate_followup(store, _follow(parent_run=rid, reuse_sources=[source_id],
                                                    workspace=str(tmp_path / "ws")),
                                     vault_public=DESTINATION, budgets=TaskDefaults())
    assert len(scope.reuse) == 1 and scope.reuse[0].origin["route"] == "workspace"
    assert scope.reuse[0].origin["source_id"] == source_id
    assert scope.reuse[0].retrieved_at and scope.reuse[0].age is not None
    assert seeds[0]["data"]["content"] == "fixture body"
    assert seeds[0]["data"]["reused_from"]["workspace_id"] == workspace.workspace_id
    assert any("工作区" in note for note in scope.notes)


def test_a_source_id_the_named_workspace_does_not_hold_is_refused(tmp_path):
    store = _store(tmp_path)
    rid, _ = _run(store)
    Workspace(tmp_path / "ws").open()
    with pytest.raises(Exception) as caught:
        validate_followup(store, _follow(parent_run=rid, reuse_sources=["src_0123456789abcdef"],
                                         workspace=str(tmp_path / "ws")),
                          vault_public=DESTINATION, budgets=TaskDefaults())
    assert caught.value.status_code == 409
    # Naming sources without a directory is refused too, rather than read from nowhere.
    with pytest.raises(Exception) as missing:
        validate_followup(store, _follow(parent_run=rid, reuse_sources=["src_0123456789abcdef"]),
                          vault_public=DESTINATION, budgets=TaskDefaults())
    assert missing.value.status_code == 422


def test_model_text_cannot_enter_a_conversation_as_reused_evidence(tmp_path):
    store = _store(tmp_path)
    rid, _ = _run(store)
    workspace = Workspace(tmp_path / "ws").open()
    from re0.workspace import WorkspaceError
    with pytest.raises(WorkspaceError):
        workspace.record({"locator": "model", "kind": "claim", "content": "a model wrote this",
                          "origin": "model"}, tool="search_papers")
    with pytest.raises(Exception):
        validate_followup(store, _follow(parent_run=rid, reuse_sources=["src_0123456789abcdef"],
                                         workspace=str(tmp_path / "ws")),
                          vault_public=DESTINATION, budgets=TaskDefaults())


def test_stale_material_is_flagged_and_never_refetched(tmp_path):
    store = _store(tmp_path)
    old = (datetime.now(timezone.utc) - timedelta(days=STALE_AFTER_DAYS + 5)).isoformat()
    rid, cid = _run(store, evidence=[{"kind": "paper", "paper": {"title": "旧材料"},
                                      "content": "body", "retrieved_at": old}])
    eid = store.evidence(rid)[0]["id"]
    scope, seeds = validate_followup(store, _follow(parent_run=rid, reuse_evidence=[eid]),
                                     vault_public=DESTINATION, budgets=TaskDefaults())
    assert scope.reuse[0].stale is True
    assert scope.stale == [eid]
    assert any("没有自动重抓" in note for note in scope.notes)
    # The carried body is the one that was stored: staleness is reported, not acted on.
    assert seeds[0]["data"]["retrieved_at"] == old


def test_an_oversized_reuse_list_is_refused_rather_than_truncated(tmp_path):
    store = _store(tmp_path)
    rid, _ = _run(store)
    # Each list is bounded at 40 by the schema, so the case that reaches the service is a caller
    # combining both. The service refuses the total rather than quietly keeping the first 40.
    with pytest.raises(Exception) as caught:
        validate_followup(store, _follow(
            parent_run=rid,
            reuse_evidence=[f"ev_{index:013d}" for index in range(40)],
            reuse_sources=[f"src_{index:016x}" for index in range(40)]),
            vault_public=DESTINATION, budgets=TaskDefaults())
    assert caught.value.status_code == 422
    assert "缩小范围" in str(caught.value.detail)
    # And the schema bound itself is not something a caller can talk its way past.
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        _follow(parent_run=rid, reuse_evidence=["ev_x"] * 41)


# ----------------------------------------------------------------------------------- two real turns

def _app(tmp_path, model):
    return TestClient(create_app(str(tmp_path / "session.sqlite3"), httpx.MockTransport(model)),
                      headers=HEADERS)


def test_a_followup_reuses_evidence_and_does_not_refetch_the_source(tmp_path):
    model = TurnModel()
    with _app(tmp_path, model) as client:
        client.put("/api/agent/config", json=CONFIG)
        first = _first_turn(client)
        assert model.arxiv_hits == 1
        ids = [item["id"] for item in first["evidence"]]
        response = _followup(client, first["id"], ids)
        assert response.status_code == 202, response.text
        second = _wait(client, response.json()["id"])
        assert second["status"] == "completed", second
        # The whole point: one retrieval, two turns.
        assert model.arxiv_hits == 1
        assert second["turn"] == 2 and second["kind"] == "followup"
        assert second["conversation_id"] == first["conversation_id"]
        assert second["report"]["findings"][0]["evidence_ids"]
        for eid in second["report"]["findings"][0]["evidence_ids"]:
            assert eid in {item["id"] for item in second["evidence"]}
        reused = [item for item in second["evidence"] if item.get("reused_from")]
        assert reused and reused[0]["reused_from"]["evidence_id"] in ids
        assert reused[0]["reused_from"]["turn"] == 1
        # The new condition is visible in the turn's own record.
        assert "训练代码" in second["origin"]["goal"]
        assert second["origin"]["permissions"]["reuse_count"] == len(ids)


def test_a_completed_followup_states_its_delta_and_leaves_the_first_report_readable(tmp_path):
    model = TurnModel()
    with _app(tmp_path, model) as client:
        client.put("/api/agent/config", json=CONFIG)
        first = _first_turn(client)
        second = _wait(client, _followup(client, first["id"],
                                         [item["id"] for item in first["evidence"]]).json()["id"])
        delta = client.get(f"/api/agent/runs/{second['id']}/delta").json()
        assert delta["against_run"] == first["id"] and delta["against_turn"] == 1
        # Reuse mints new ids, so the map is what keeps a carried finding from reading as churn.
        assert delta["added"] == [] and delta["dropped"] == []
        assert len(delta["changed"]) == 1
        assert delta["changed"][0]["to"]["claim"].startswith("追问轮")
        # The earlier report is untouched and still exportable on its own.
        again = client.get(f"/api/agent/runs/{first['id']}").json()
        assert again["report"]["findings"][0]["claim"].startswith("首轮")
        assert again["status"] == "completed"
        export = client.get(f"/api/agent/runs/{first['id']}/export").text
        assert "首轮" in export and CONFIG["api_key"] not in export


def test_an_approved_import_from_the_first_turn_survives_the_second(tmp_path):
    model = TurnModel()
    with _app(tmp_path, model) as client:
        client.put("/api/agent/config", json=CONFIG)
        first = _first_turn(client)
        evidence_id = first["evidence"][0]["id"]
        path = f"/api/agent/runs/{first['id']}/evidence/{evidence_id}/import"
        assert client.post(path, json={"confirmed": True}).json()["created"] is True
        paper = client.get("/api/papers").json()[0]
        # PaperInput forbids extras, so the editable fields are named rather than echoed back whole.
        editable = {key: paper[key] for key in ("title", "authors", "year", "venue", "abstract", "doi",
                                                "arxiv_id", "paper_url", "topics", "status", "notes",
                                                "version_label") if key in paper}
        revised = client.put(f"/api/papers/{paper['id']}", json={**editable, "notes": "我的人工批注"})
        assert revised.status_code == 200, revised.text
        second = _wait(client, _followup(client, first["id"], [evidence_id]).json()["id"])
        assert second["status"] == "completed"
        kept = client.get(f"/api/papers/{paper['id']}").json()
        assert kept["notes"] == "我的人工批注"
        assert len(client.get("/api/papers").json()) == 1
        # Approving the same reused evidence again does not mint a second paper.
        reused_id = [item["id"] for item in second["evidence"] if item.get("reused_from")][0]
        again = client.post(f"/api/agent/runs/{second['id']}/evidence/{reused_id}/import",
                            json={"confirmed": True}).json()
        assert again["created"] is False and again["paper_id"] == paper["id"]
        assert len(client.get("/api/papers").json()) == 1


def test_a_duplicate_submit_returns_the_turn_it_already_created(tmp_path):
    model = TurnModel()
    with _app(tmp_path, model) as client:
        client.put("/api/agent/config", json=CONFIG)
        first = _first_turn(client)
        ids = [item["id"] for item in first["evidence"]]
        body = {"goal": "只保留有训练代码的两篇", "reuse_evidence": ids, "authorize_spend": True,
                "consent_to_send": True, "idempotency_key": "same-key"}
        one = client.post("/api/agent/followups", json={**body, "parent_run": first["id"]}).json()
        _wait(client, one["id"])
        two = client.post("/api/agent/followups", json={**body, "parent_run": first["id"]}).json()
        assert two["id"] == one["id"]
        conversation = client.get(f"/api/agent/conversations/{one['conversation_id']}").json()
        assert len(conversation["runs"]) == 2
        assert any(event["kind"] == "idempotent_replay"
                   for event in client.get(f"/api/agent/runs/{one['id']}/events").json())


def test_the_session_cap_stops_a_turn_mid_flight_instead_of_overspending(tmp_path):
    model = TurnModel()
    with _app(tmp_path, model) as client:
        client.put("/api/agent/config", json=CONFIG)
        assert client.put("/api/agent/session-defaults",
                          json={"max_session_model_calls": 4, "max_session_tool_calls": 40}).status_code == 200
        first = _first_turn(client)
        assert first["model_calls"] == 3
        second = _wait(client, _followup(client, first["id"],
                                         [item["id"] for item in first["evidence"]],
                                         max_model_calls=8).json()["id"])
        assert second["status"] == "budget_exhausted", second
        assert second["model_calls"] == 1
        assert "会话累计模型调用已达上限" in second["error"]
        conversation = client.get(f"/api/agent/conversations/{first['conversation_id']}").json()
        assert conversation["ledger"]["model_calls"] == 4
        # A third turn is refused at admission: the cap is cumulative and was not reset.
        assert _followup(client, first["id"], []).status_code == 409


def test_a_retry_after_a_failure_continues_the_same_ledger(tmp_path):
    model = TurnModel()
    with _app(tmp_path, model) as client:
        client.put("/api/agent/config", json=CONFIG)
        run = client.post("/api/agent/runs",
                          json={**GOAL, "max_model_calls": 2, "max_tool_calls": 2}).json()
        stopped = _wait(client, run["id"])
        assert stopped["status"] == "budget_exhausted"
        response = client.post("/api/agent/retries", json={
            "parent_run": run["id"], "authorize_spend": True, "consent_to_send": True,
            "idempotency_key": "retry-once"})
        assert response.status_code == 202, response.text
        retried = _wait(client, response.json()["id"])
        assert retried["kind"] == "retry" and retried["turn"] == 2
        assert retried["goal"] == stopped["goal"]
        conversation = client.get(f"/api/agent/conversations/{run['conversation_id']}").json()
        assert conversation["ledger"]["model_calls"] == stopped["model_calls"] + retried["model_calls"]
        assert [t["kind"] for t in conversation["runs"]] == ["new", "retry"]


def test_a_scope_preview_creates_nothing_and_spends_nothing(tmp_path):
    model = TurnModel()
    with _app(tmp_path, model) as client:
        client.put("/api/agent/config", json=CONFIG)
        first = _first_turn(client)
        calls_before = model.calls
        ids = [item["id"] for item in first["evidence"]]
        preview = client.post("/api/agent/followups/scope", json={
            "parent_run": first["id"], "goal": "只保留有训练代码的两篇", "reuse_evidence": ids,
            "authorize_spend": True, "consent_to_send": True})
        assert preview.status_code == 200, preview.text
        body = preview.json()
        assert body["turn"] == 2 and body["kind"] == "followup"
        assert len(body["reuse"]) == len(ids)
        assert body["ledger"]["model_calls"] == first["model_calls"]
        assert body["model_destination"]["model"] == CONFIG["model"]
        assert "没有发起任何模型或网络调用" in body["note"]
        assert model.calls == calls_before
        assert len(client.get(f"/api/agent/conversations/{first['conversation_id']}").json()["runs"]) == 1


def test_a_conversation_export_holds_every_report_and_no_transcript_or_key(tmp_path):
    model = TurnModel()
    with _app(tmp_path, model) as client:
        client.put("/api/agent/config", json=CONFIG)
        first = _first_turn(client)
        second = _wait(client, _followup(client, first["id"],
                                         [item["id"] for item in first["evidence"]]).json()["id"])
        export = client.get(f"/api/agent/conversations/{first['conversation_id']}/export")
        assert export.status_code == 200
        body = export.json()
        assert [turn["turn"] for turn in body["turns"]] == [1, 2]
        assert body["turns"][0]["report"]["findings"][0]["claim"].startswith("首轮")
        assert body["turns"][1]["report_delta"]["against_turn"] == 1
        text = export.text
        assert CONFIG["api_key"] not in text
        assert "messages" not in body["turns"][0]
        assert "content" not in json.dumps(body["turns"][1]["evidence"])


def test_the_immutable_snapshot_is_readable_and_carries_no_secret(tmp_path):
    model = TurnModel()
    with _app(tmp_path, model) as client:
        client.put("/api/agent/config", json=CONFIG)
        first = _first_turn(client)
        second = _wait(client, _followup(client, first["id"],
                                         [item["id"] for item in first["evidence"]]).json()["id"])
        origin = client.get(f"/api/agent/runs/{second['id']}/origin").json()
        assert origin["kind"] == "followup" and origin["turn"] == 2
        assert origin["parent_run"] == first["id"]
        assert origin["model_destination"]["model"] == CONFIG["model"]
        assert origin["permissions"]["use_library"] is False
        assert "search_papers" in origin["permissions"]["allowed_tools"]
        assert origin["budgets"]["max_model_calls"] >= 2
        assert CONFIG["api_key"] not in json.dumps(origin)
        # A turn's own record cannot be edited by the turn that follows it.
        assert client.get(f"/api/agent/runs/{first['id']}/origin").json()["turn"] == 1


def test_the_console_reads_a_conversation_and_previews_a_turn_without_spending(tmp_path, capsys):
    """`re0 session` shares the service, so the console can validate a turn it cannot start."""
    from re0 import session_cli

    model = TurnModel()
    db = str(tmp_path / "session.sqlite3")
    with _app(tmp_path, model) as client:
        client.put("/api/agent/config", json=CONFIG)
        first = _first_turn(client)
        second = _wait(client, _followup(client, first["id"],
                                         [item["id"] for item in first["evidence"]]).json()["id"])
    conversation, ids = first["conversation_id"], [item["id"] for item in first["evidence"]]
    calls = model.calls

    assert session_cli.main(["list", "--db", db]) == 0
    listed = capsys.readouterr().out
    assert conversation in listed and "模型/工具累计" in listed

    assert session_cli.main(["show", conversation, "--db", db]) == 0
    shown = capsys.readouterr().out
    assert first["id"] in shown and second["id"] in shown
    assert "followup" in shown and "上限" in shown and "累计" in shown
    assert "不会被新一轮重置" in shown or "累计" in shown

    assert session_cli.main(["delta", second["id"], "--db", db]) == 0
    compared = capsys.readouterr().out
    assert "追问轮" in compared and "由不确定转为有结论" in compared

    assert session_cli.main(["scope", first["id"], "--db", db,
                             "--goal", "只保留有训练代码的两篇，并补查它们的数据划分",
                             "--reuse-evidence", ids[0]]) == 0
    preview = capsys.readouterr().out
    # Two turns already exist, so the preview must promise the third rather than a second.
    assert "第 3 轮" in preview and "followup" in preview
    assert "第 1 轮" in preview  # the parent turn it continues
    assert "不会重新抓取" in preview and "没有创建任务" in preview
    assert "无法比较" in preview  # the console holds no key, so it says so instead of guessing
    # A read command and a preview must not have touched the model.
    assert model.calls == calls

    assert session_cli.main(["follow-up", first["id"], "--dry-run",
                             "--goal", "只保留有训练代码的两篇", "--reuse-evidence", ids[0],
                             "--use-library", "--idempotency-key", "cli-1"]) == 0
    dry = capsys.readouterr().out
    assert "would POST" in dry and '"authorize_spend": true' in dry
    assert '"use_library": true' in dry and '"idempotency_key": "cli-1"' in dry
    assert model.calls == calls

    # With no service listening, the refusal is plain and nothing was created.
    assert session_cli.main(["follow-up", first["id"], "--goal", "只保留有训练代码的两篇",
                             "--base-url", "http://127.0.0.1:1"]) == 1
    refused = capsys.readouterr().err
    assert "连不上本地服务" in refused and "没有花费" in refused

    # A missing database is reported, not created by a read command.
    assert session_cli.main(["list", "--db", str(tmp_path / "absent.sqlite3")]) == 2
    assert "没有任务数据库" in capsys.readouterr().err
    assert not (tmp_path / "absent.sqlite3").exists()


def test_a_write_claiming_to_be_the_console_is_refused_when_it_carries_a_browser_origin(tmp_path):
    model = TurnModel()
    with _app(tmp_path, model) as client:
        client.put("/api/agent/config", json=CONFIG)
        first = _first_turn(client)
        body = {"parent_run": first["id"], "goal": "追问一下这两篇的数据划分",
                "authorize_spend": True, "consent_to_send": True}
        # The console client id is only honoured without a browser origin: a page cannot borrow it.
        forged = client.post("/api/agent/followups", json=body,
                             headers={**CLI_HEADERS, "Origin": "https://evil.example"})
        assert forged.status_code == 403
        referer = client.post("/api/agent/followups", json=body,
                              headers={**CLI_HEADERS, "Referer": "https://evil.example/"})
        assert referer.status_code == 403
        assert len(client.get(f"/api/agent/conversations/{first['conversation_id']}").json()["runs"]) == 1
