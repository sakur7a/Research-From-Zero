"""`re0 session` — inspect and continue a research conversation from the console.

This shares one service with the API and the web UI (`re0.agent.session`); nothing here depends on a
browser and nothing here starts a model. Two things follow from that split:

* `list`, `show`, `delta` and `scope` read the task database directly. `scope` runs the *same*
  validation the API runs before it creates a turn, so it answers "what would this follow-up be
  allowed to touch, and what would it cost" without authorizing any of it.
* Actually starting a turn needs a model key, and keys live in server memory by design — they are
  never written to disk where a CLI could read them. So `follow-up` posts to the running local
  service instead of building a model here, and says so when the service is not there.

One thing the console genuinely cannot check: whether this turn's model destination matches the
parent's. The key and the configured endpoint are in the server's memory, not in the database, so
`scope` reports the destination as unknown here and the service re-checks it when the turn is
created.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

from pydantic import ValidationError

from .agent.schemas import FollowUpInput, RetryInput, TaskDefaults
from .agent.session import validate_followup, validate_retry
from .deployment import LOCAL_OWNER

PROGRAM = "re0 session"
ROOT = Path(__file__).resolve().parents[2]
DEFAULT_BASE_URL = "http://127.0.0.1:8000"
DEFAULTS_KEY = "task_defaults"
# The console reads the database directly, so it acts as the owner a single-user database has: the
# local one. It cannot act for a hosted account, which holds its identity in a session cookie the
# console has no way to present — that is why hosted mode refuses requests claiming `X-Re0-Client:
# cli` instead of letting them in as an owner they cannot prove.
# The CLI holds no key, so it cannot know where a turn would be sent. An empty destination makes the
# service-side check the only one that counts, and says so rather than pretending to have compared.
UNCONFIGURED_DESTINATION: dict = {}


def database_path(explicit: str = "") -> str:
    return explicit or os.getenv("RE0_DB") or str(ROOT / ".data" / "re0.sqlite3")


def _store(path: str):
    from .agent.storage import TaskStore
    from .db import Database

    if not Path(path).is_file():
        raise SystemExit(f"没有任务数据库：{path}\n先启动一次本地服务（它会创建并迁移这个文件），"
                         "或用 --db 指向已有的那一个。")
    return TaskStore(Database(path))


def _defaults(store) -> TaskDefaults:
    try:
        return TaskDefaults(**store.setting(DEFAULTS_KEY, owner=LOCAL_OWNER))
    except ValidationError:
        # A hand-edited row must not stop a read command, and must not widen a budget.
        return TaskDefaults()


def _followup_params(arguments) -> FollowUpInput:
    return FollowUpInput(parent_run=arguments.parent_run, goal=arguments.goal,
                         reuse_evidence=arguments.reuse_evidence or [],
                         reuse_sources=arguments.reuse_source or [],
                         workspace=arguments.workspace or "",
                         use_library=arguments.use_library or None,
                         max_model_calls=arguments.max_model_calls,
                         max_tool_calls=arguments.max_tool_calls,
                         attempt_seconds=arguments.attempt_seconds,
                         authorize_spend=True, consent_to_send=True,
                         trust_new_destination=arguments.trust_new_destination,
                         idempotency_key=arguments.idempotency_key or "")


def _print_scope(scope, budgets) -> None:
    print(f"会话 {scope.conversation_id} ｜ 本轮将是第 {scope.turn} 轮（{scope.kind}）")
    print(f"父任务 {scope.parent_run}（第 {scope.parent_turn} 轮）")
    print(f"目标：{scope.goal}")
    print()
    print(f"本轮预算：model_calls={budgets.max_model_calls} tool_calls={budgets.max_tool_calls} "
          f"attempt_seconds={budgets.attempt_seconds} use_library={budgets.use_library}")
    ledger = scope.ledger
    print(f"会话累计（不会被新一轮重置）：model_calls={ledger['model_calls']}/{scope.caps.get('max_session_model_calls')} "
          f"tool_calls={ledger['tool_calls']}/{scope.caps.get('max_session_tool_calls')} "
          f"turns={ledger['turns']} 未回报用量={ledger['unreported_calls']}")
    print()
    if scope.reuse:
        print(f"将复用的历史材料（{len(scope.reuse)} 条，本轮不会重新抓取）：")
        for item in scope.reuse:
            age = f"{item.age:.0f} 天前" if item.age is not None else "来源时间未知"
            flag = "  ← 已过时阈值，是否付费补查由你决定" if item.stale else ""
            route = item.origin.get("route", "")
            where = f"第 {item.origin.get('turn', '?')} 轮" if route == "conversation" \
                else f"工作区 {item.origin.get('source_id', '')}"
            print(f"  {where} ｜ {age}{flag}\n    {item.summary}")
    else:
        print("将复用的历史材料：无（本轮需要自己取证）")
    if scope.notes:
        print()
        print("作用域说明：")
        for note in scope.notes:
            print(f"  - {note}")
    print()
    print("模型目的地：本机 CLI 没有密钥，无法比较；发起本轮时由服务重新检查。")
    print("以上是作用域校验结果：没有创建任务，没有发起模型或网络调用，没有花费。")


def session_command(arguments) -> int:
    action = getattr(arguments, "action", "")
    try:
        if action in {"list", "show", "delta", "scope"}:
            store = _store(database_path(arguments.db))
        if action == "list":
            rows = store.conversations(owner=LOCAL_OWNER)
            if not rows:
                print("还没有会话。用 `re0 paper search` 检索，或在 Web 工作台里发起一个研究任务。")
                return 0
            print(f"{'会话':<22} {'轮次':>4} {'模型/工具累计':>14}  目标")
            for row in rows:
                ledger = row["ledger"]
                print(f"{row['id']:<22} {ledger['turns']:>4} "
                      f"{str(ledger['model_calls']) + '/' + str(ledger['tool_calls']):>14}  "
                      f"{row['goal'][:60]}")
            return 0
        if action == "show":
            conversation = store.conversation(arguments.conversation_id, owner=LOCAL_OWNER)
            caps, ledger = conversation["caps"], conversation["ledger"]
            print(f"会话 {conversation['id']}")
            print(f"目标：{conversation['goal']}")
            print(f"创建于 {conversation['created_at']}，最近更新 {conversation['updated_at']}")
            if conversation.get("note"):
                print(f"说明：{conversation['note']}")
            print(f"上限：model_calls={caps.get('max_session_model_calls')} "
                  f"tool_calls={caps.get('max_session_tool_calls')}")
            print(f"累计：model_calls={ledger['model_calls']} tool_calls={ledger['tool_calls']} "
                  f"turns={ledger['turns']} 未回报用量={ledger['unreported_calls']}")
            print(f"{'轮':>3} {'类型':<9} {'状态':<17} {'模型':>4} {'工具':>4}  run id")
            for run in conversation["runs"]:
                print(f"{run['turn']:>3} {run['kind']:<9} {run['status']:<17} "
                      f"{run['model_calls']:>4} {run['tool_calls']:>4}  {run['id']}"
                      + ("  （有报告）" if run["has_report"] else ""))
            print()
            print("每一轮的报告与证据都能单独导出：GET /api/agent/runs/<run id>/export")
            return 0
        if action == "delta":
            stored = store.get(arguments.run_id, owner=LOCAL_OWNER)
            delta = stored.get("report_delta")
            if not delta:
                print("这一轮没有已完成的报告，或它是会话的第一轮：没有可对比的上一轮。")
                return 0
            print(f"第 {stored.get('turn')} 轮相对第 {delta['against_turn']} 轮"
                  f"（run {delta['against_run']}）")
            print(f"结论状态：{delta['outcome']['previous'] or '（无）'} → {delta['outcome']['current']}")
            for label, key in (("新增", "added"), ("仍不确定", "still_uncertain"), ("本轮未再提", "dropped")):
                entries = delta[key]
                print(f"\n{label}（{len(entries)}）：")
                for entry in entries:
                    print(f"  [{entry['assessment']}] {entry['claim']}")
            print(f"\n改变（{len(delta['changed'])}）：")
            for entry in delta["changed"]:
                print(f"  旧 [{entry['from']['assessment']}] {entry['from']['claim']}")
                print(f"  新 [{entry['to']['assessment']}] {entry['to']['claim']}")
            print(f"\n由不确定转为有结论（{len(delta['resolved_from_uncertain'])}）：")
            for entry in delta["resolved_from_uncertain"]:
                print(f"  {entry['to']['claim']}")
            print(f"\n{delta['note']}")
            return 0
        if action == "scope":
            params = _followup_params(arguments)
            budgets = _defaults(store).merged(params)
            scope, _ = validate_followup(store, params, owner=LOCAL_OWNER,
                                         vault_public=UNCONFIGURED_DESTINATION, budgets=budgets)
            if arguments.json:
                payload = {**scope.as_dict(), "budgets": budgets.model_dump()}
                Path(arguments.json).write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                                                encoding="utf-8")
                print(f"wrote {arguments.json}")
                return 0
            _print_scope(scope, budgets)
            return 0
        if action == "follow-up":
            return _post_turn("/api/agent/followups", _followup_payload(arguments), arguments)
        if action == "retry":
            payload = {"parent_run": arguments.parent_run, "authorize_spend": True,
                       "consent_to_send": True, "trust_new_destination": arguments.trust_new_destination,
                       "idempotency_key": arguments.idempotency_key or ""}
            for name in ("max_model_calls", "max_tool_calls", "attempt_seconds"):
                if getattr(arguments, name) is not None:
                    payload[name] = getattr(arguments, name)
            return _post_turn("/api/agent/retries", payload, arguments)
    except ValidationError as exc:
        print("参数不符合契约：", file=sys.stderr)
        for error in exc.errors()[:10]:
            print(f"  {'.'.join(map(str, error['loc']))}: {error['msg']}", file=sys.stderr)
        return 2
    except SystemExit as exc:
        print(exc, file=sys.stderr)
        return 2
    print("pass one of: list, show ID, delta RUN, scope PARENT --goal TEXT, "
          "follow-up PARENT --goal TEXT, retry PARENT", file=sys.stderr)
    return 2


def _followup_payload(arguments) -> dict:
    """The request body, spelled out so `--dry-run` shows exactly what would be authorized."""
    payload = {"parent_run": arguments.parent_run, "goal": arguments.goal,
               "reuse_evidence": arguments.reuse_evidence or [],
               "reuse_sources": arguments.reuse_source or [], "workspace": arguments.workspace or "",
               "authorize_spend": True, "consent_to_send": True,
               "trust_new_destination": arguments.trust_new_destination,
               "idempotency_key": arguments.idempotency_key or ""}
    if arguments.use_library:
        payload["use_library"] = True
    for name in ("max_model_calls", "max_tool_calls", "attempt_seconds"):
        if getattr(arguments, name) is not None:
            payload[name] = getattr(arguments, name)
    if arguments.raise_session_model_calls or arguments.raise_session_tool_calls:
        caps = {}
        if arguments.raise_session_model_calls:
            caps["max_session_model_calls"] = arguments.raise_session_model_calls
        if arguments.raise_session_tool_calls:
            caps["max_session_tool_calls"] = arguments.raise_session_tool_calls
        payload["raise_session_caps"] = caps
    return payload


def _post_turn(path: str, payload: dict, arguments) -> int:
    url = arguments.base_url.rstrip("/") + path
    if arguments.dry_run:
        print(f"would POST {url}")
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        print()
        print("dry run: nothing was sent. 发起一轮需要模型密钥，而密钥只存在于运行中的服务内存里，"
              "所以这里不会自己起一个模型。")
        return 0
    request = urllib.request.Request(
        url, data=json.dumps(payload, ensure_ascii=False).encode("utf-8"), method="POST",
        # `cli` rather than `web`: the service refuses a write that claims to be the console while
        # carrying a browser Origin header, so this cannot be forged by a page.
        headers={"Content-Type": "application/json", "X-Re0-Client": "cli"})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            body = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:800]
        print(f"服务拒绝了这一轮（HTTP {exc.code}）：{detail}", file=sys.stderr)
        return 1
    except (urllib.error.URLError, TimeoutError, ValueError) as exc:
        print(f"连不上本地服务 {url}（{type(exc).__name__}）。\n先启动它，再重试；"
              "这一轮没有创建，也没有花费。", file=sys.stderr)
        return 1
    print(f"已创建第 {body.get('turn')} 轮（{body.get('kind')}）：run {body.get('id')} "
          f"状态 {body.get('status')}")
    print(f"会话 {body.get('conversation_id')}；进度用 GET {arguments.base_url}/api/agent/runs/"
          f"{body.get('id')}/events 查看")
    print("发起一轮会花费模型调用；累计账本记在会话上，不会因为新建一轮而重置。")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog=PROGRAM, description=__doc__.splitlines()[0])
    actions = parser.add_subparsers(dest="action")

    for name in ("list",):
        sub = actions.add_parser(name, help="list research conversations with their cumulative ledger")
        sub.add_argument("--db", default="", help="task database path (default: $RE0_DB or .data/re0.sqlite3)")

    show = actions.add_parser("show", help="one conversation: caps, cumulative ledger, every turn")
    show.add_argument("conversation_id")
    show.add_argument("--db", default="")

    delta = actions.add_parser("delta", help="what one turn's report added, changed or left open")
    delta.add_argument("run_id")
    delta.add_argument("--db", default="")

    scope = actions.add_parser("scope",
                               help="validate a follow-up without creating it; spends nothing")
    _turn_arguments(scope, goal=True, reuse=True)
    scope.add_argument("--json", default="", metavar="PATH", help="write the validation result here")
    scope.add_argument("--db", default="")

    follow = actions.add_parser("follow-up",
                                help="start a follow-up turn through the running local service")
    _turn_arguments(follow, goal=True, reuse=True)
    follow.add_argument("--raise-session-model-calls", type=int, default=0)
    follow.add_argument("--raise-session-tool-calls", type=int, default=0)
    _service_arguments(follow)

    retry = actions.add_parser("retry", help="repeat a stopped turn's goal verbatim, as a new turn")
    _turn_arguments(retry, goal=False, reuse=False)
    retry.add_argument("--raise-session-model-calls", type=int, default=0)
    retry.add_argument("--raise-session-tool-calls", type=int, default=0)
    _service_arguments(retry)
    return parser


def _turn_arguments(parser, *, goal: bool, reuse: bool) -> None:
    parser.add_argument("parent_run", help="the run this turn continues")
    if goal:
        parser.add_argument("--goal", required=True,
                            help="what this turn adds or changes; a retry has no goal because it "
                                 "repeats the parent's verbatim")
    if reuse:
        parser.add_argument("--reuse-evidence", action="append", default=[], metavar="ID",
                            help="evidence id from an earlier turn of the same conversation. An id "
                                 "from another conversation is refused, not ignored.")
        parser.add_argument("--reuse-source", action="append", default=[], metavar="SRC_ID",
                            help="content-addressed source id recorded in a workspace")
        parser.add_argument("--workspace", default="", metavar="DIR",
                            help="workspace directory holding --reuse-source ids")
        parser.add_argument("--use-library", action="store_true",
                            help="authorize sending local bibliography metadata this turn. It is "
                                 "never inherited from an earlier one.")
    parser.add_argument("--max-model-calls", type=int, default=None)
    parser.add_argument("--max-tool-calls", type=int, default=None)
    parser.add_argument("--attempt-seconds", type=int, default=None)
    parser.add_argument("--trust-new-destination", action="store_true",
                        help="required when this turn's model endpoint differs from the parent's: "
                             "history would travel to a provider you have not used for it before")
    parser.add_argument("--idempotency-key", default="",
                        help="a repeated submit with the same key returns the turn it already "
                             "created instead of starting a second one")


def _service_arguments(parser) -> None:
    parser.add_argument("--base-url", default=os.getenv("RE0_BASE_URL", DEFAULT_BASE_URL),
                        help=f"local service to post to (default {DEFAULT_BASE_URL})")
    parser.add_argument("--dry-run", action="store_true",
                        help="print the request body and send nothing")


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
    return session_command(arguments)
