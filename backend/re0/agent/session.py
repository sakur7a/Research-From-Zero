"""Continuing a research conversation: follow-ups, changed constraints, auditable reruns.

A *run* is one attempt. A *conversation* is the line of attempts that share evidence and one
cumulative ledger. Three kinds of turn are told apart because they authorize different things:

* ``new`` — a fresh question. It starts its own conversation and its own ledger.
* ``followup`` — a new constraint on work already done ("keep only the two with training code, and
  check their data splits"). It may reuse named evidence instead of re-fetching it.
* ``retry`` — the same request again after a failure. It repeats the parent's goal verbatim, so it
  cannot carry new scope; new scope is a follow-up, and a follow-up has to name what it reuses.

Two rules shape everything here. **Scope is validated before the model sees any history**, so an
unauthorized reuse is refused rather than quietly dropped — a silently ignored constraint reads as
"checked, nothing found". And **the ledger is cumulative across turns**, recomputed from the runs
each time rather than incremented, so a follow-up cannot escape a cap by being a new run.

This module needs no browser and starts no model: the API, the CLI and the MCP surface all call the
same functions.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone

from fastapi import HTTPException

from ..models import now

TURN_KINDS = ("new", "followup", "retry")
# A turn may only be added to a conversation whose last turn has stopped. Running two turns at once
# would interleave two ledgers and two reports over the same evidence.
OPEN_STATUSES = ("queued", "running")
RETRYABLE_STATUSES = ("failed", "interrupted", "budget_exhausted", "cancelled")
STALE_AFTER_DAYS = 30
REUSE_LIMIT = 40
REUSE_EXCERPT_CHARS = 400
REUSE_EXCERPT_TOTAL = 6000
# Destination fields compared when deciding whether old material would travel somewhere new.
DESTINATION_FIELDS = ("base_url", "model", "token_parameter", "max_output_tokens")
CLAIM_CHARS = 300


def _parse_stamp(value) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return stamp if stamp.tzinfo else stamp.replace(tzinfo=timezone.utc)


def age_days(stamp, *, reference: datetime | None = None) -> float | None:
    """Age in days, or None when the source did not give a parseable time.

    None is not 0: material of unknown age is not fresh material, and calling it fresh would hide
    the one case where a refresh is most needed.
    """
    parsed = _parse_stamp(stamp)
    if parsed is None:
        return None
    return ((reference or datetime.now(timezone.utc)) - parsed).total_seconds() / 86400.0


@dataclass
class Reuse:
    """One item of history this turn is allowed to build on."""

    evidence_id: str
    origin: dict
    retrieved_at: str = ""
    age: float | None = None
    stale: bool = False
    summary: str = ""
    excerpt: str = ""
    kind: str = ""
    tool: str = ""

    def as_dict(self) -> dict:
        return {"evidence_id": self.evidence_id, "origin": self.origin,
                "retrieved_at": self.retrieved_at,
                "age_days": round(self.age, 1) if self.age is not None else None,
                "stale": self.stale, "summary": self.summary, "kind": self.kind, "tool": self.tool}

    @property
    def origin_id(self) -> str:
        """Whichever id a caller can act on right now.

        Before the turn exists the material has only the id it came from; after seeding it also has
        the id it will be cited by inside the new turn. Reporting the empty string in the first case
        would make a staleness warning point at nothing.
        """
        return self.evidence_id or str(self.origin.get("evidence_id")
                                       or self.origin.get("source_id") or "")


@dataclass
class Scope:
    """What a turn is allowed to touch, decided before any model call.

    `existing_run` is set when an idempotency key matched: the caller should return that run instead
    of starting a turn, which is what makes a double click or a retried POST cost nothing.
    """

    conversation_id: str
    parent_run: str
    parent_turn: int
    turn: int
    kind: str
    goal: str
    reuse: list[Reuse] = field(default_factory=list)
    authorizations: dict = field(default_factory=dict)
    caps: dict = field(default_factory=dict)
    ledger: dict = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    existing_run: str = ""

    @property
    def stale(self) -> list[str]:
        """Ids of reused material past the staleness threshold. Reported, never acted on."""
        return [item.origin_id for item in self.reuse if item.stale]

    def as_dict(self) -> dict:
        return {"conversation_id": self.conversation_id, "parent_run": self.parent_run,
                "parent_turn": self.parent_turn, "turn": self.turn, "kind": self.kind, "goal": self.goal,
                "reuse": [item.as_dict() for item in self.reuse],
                "stale": self.stale,
                "authorizations": self.authorizations, "caps": self.caps, "ledger": self.ledger,
                "notes": self.notes, "existing_run": self.existing_run,
                "note": "这是作用域校验的结果，不是执行结果；没有发起任何模型或网络调用。"}


def _summary(data: dict) -> str:
    """One line naming the material, so a reuse list is readable without opening every body."""
    paper = data.get("paper") or {}
    if isinstance(paper, dict) and paper.get("title"):
        venue = data.get("venue") or paper.get("venue") or ""
        year = paper.get("year") or ""
        identifier = paper.get("doi") or paper.get("arxiv_id") or ""
        return "《%s》%s%s" % (str(paper["title"])[:160],
                              f"（{venue} {year}）".strip() if venue or year else "",
                              f" ｜ {identifier}" if identifier else "")
    for key in ("locator", "source_url", "identifier", "repository", "url"):
        if data.get(key):
            return f"{key}: {str(data[key])[:200]}"
    return data.get("kind") or "source"


def _excerpt(data: dict) -> str:
    for key in ("content", "abstract"):
        body = data.get(key)
        if isinstance(body, str) and body.strip():
            return body.strip()[:REUSE_EXCERPT_CHARS]
    paper = data.get("paper") or {}
    if isinstance(paper, dict):
        abstract = paper.get("abstract")
        if isinstance(abstract, str) and abstract.strip():
            return abstract.strip()[:REUSE_EXCERPT_CHARS]
    return ""


def _reuse_row(data: dict, origin: dict) -> Reuse:
    retrieved = str(data.get("retrieved_at") or "")
    age = age_days(retrieved)
    stale = age is not None and age > STALE_AFTER_DAYS
    body = _excerpt(data)
    return Reuse(evidence_id="", origin=origin, retrieved_at=retrieved, age=age, stale=stale,
                 summary=_summary(data), excerpt=body, kind=str(data.get("kind") or ""),
                 tool=str(data.get("tool") or "")), data


def collect_reuse(store, params, conversation_id: str) -> tuple[list[Reuse], list[dict], list[str]]:
    """Resolve the named ids to evidence bodies. Returns (reuse, seed_rows, notes).

    Refusals are errors, not omissions. An id from another conversation says so explicitly, because
    "not found" would send the caller looking for a typo where the real problem is scope.
    """
    reuse: list[Reuse] = []
    seeds: list[dict] = []
    notes: list[str] = []
    wanted = list(dict.fromkeys([*(params.reuse_evidence or []), *(params.reuse_sources or [])]))
    if len(wanted) > REUSE_LIMIT:
        raise HTTPException(422, f"一轮最多复用 {REUSE_LIMIT} 条材料；请缩小范围，其余留到下一轮")
    for eid in params.reuse_evidence or []:
        owner = store.evidence_owner(eid)
        if owner is None:
            raise HTTPException(404, f"证据 {eid} 不存在；复用只能指向本会话真实记录过的材料")
        if owner["conversation_id"] != conversation_id:
            raise HTTPException(409, f"证据 {eid} 属于另一个会话（{owner['conversation_id'][:12]}…）；"
                                     "跨会话复用请先把它导出到工作区，再按 source id 引入")
        item, data = _reuse_row(owner["data"], {"route": "conversation", "run_id": owner["run_id"],
                                               "turn": owner["turn"], "evidence_id": eid})
        reuse.append(item)
        seeds.append({"data": {**data, "reused_from": item.origin, "reuse_note": REUSE_NOTE}, "reused_from": item.origin})
    if params.reuse_sources:
        notes.extend(_collect_workspace(store, params, reuse, seeds))
    return reuse, seeds, notes


REUSE_NOTE = ("复用自更早的轮次：来源时间、版本与父轮记录原样保留，本轮没有重新抓取。"
              "它仍是工具返回的观察，不是人工确认的结论。")


def _collect_workspace(store, params, reuse: list, seeds: list) -> list[str]:
    """Bring in sources a trusted tool recorded into an opt-in workspace.

    The workspace itself enforces the two boundaries that matter here: only tool-returned payloads
    are stored at all, and an id from a different workspace is refused rather than merged.
    """
    from ..workspace import Workspace, WorkspaceError

    if not params.workspace:
        raise HTTPException(422, "复用 source id 需要同时给出 workspace 目录；没有目录就没有可读的来源")
    try:
        workspace = Workspace(params.workspace).open()
    except (WorkspaceError, OSError) as exc:
        raise HTTPException(422, f"工作区打不开：{exc}") from exc
    notes = [f"工作区 {workspace.workspace_id} 被显式引入；其中 source id 与 content 一一对应"]
    for sid in params.reuse_sources:
        try:
            document = workspace.read(sid)
        except WorkspaceError as exc:
            raise HTTPException(409, str(exc)) from exc
        origin = {"route": "workspace", "workspace_id": workspace.workspace_id, "source_id": sid,
                  "tool": document.get("tool", "")}
        item, data = _reuse_row(document, origin)
        reuse.append(item)
        seeds.append({"data": {**data, "reused_from": origin, "reuse_note": REUSE_NOTE},
                      "reused_from": origin})
    return notes


def check_destination(parent_config: dict, vault_public: dict, trusted: bool) -> list[str]:
    """Refuse to move history to a provider the user has not agreed to for this turn.

    An empty `vault_public` means the caller cannot see the destination at all — the console has no
    key, since keys live only in server memory. That is reported as unchecked rather than treated as
    "same" or as "different": guessing either way would be a claim we cannot support, and the service
    runs this check again with the real configuration when the turn is created.
    """
    if not vault_public:
        return ["本机看不到模型目的地（密钥只在服务内存里），所以这一项没有比较；"
                "创建本轮时服务会用真实配置重新检查"]
    differences = [name for name in DESTINATION_FIELDS
                   if str(parent_config.get(name, "")) != str(vault_public.get(name, ""))]
    if not differences:
        return []
    if not trusted:
        raise HTTPException(409, "本轮模型目的地与上一轮不同（%s）；复用历史材料前需要显式确认 "
                                 "trust_new_destination，否则旧内容会被发往你没有同意的服务"
                            % "、".join(differences))
    return ["模型目的地相对上一轮发生变化：%s；已按要求显式确认，本轮的历史材料会发往新目的地"
            % "、".join(f"{name} {parent_config.get(name)!r} → {vault_public.get(name)!r}"
                        for name in differences)]


def check_ledger(store, conversation_id: str, budgets, params,
                 scope_notes: list[str]) -> tuple[dict, dict]:
    """Enforce the cumulative caps. Returns the ledger before this turn and the caps that apply.

    Raising a cap is allowed but has to be asked for in the same request that needs it. The new caps
    are returned rather than written here: validation must not mutate anything, because a later check
    can still refuse the turn, and a cap raised by a refused request would be a cap nobody asked for.
    The caller applies them when the turn is created, and the ask is recorded in its snapshot — so
    "the cap moved" is always attributable to a turn that actually ran.
    """
    ledger = store.ledger(conversation_id)
    caps = dict(store.conversation(conversation_id)["caps"])
    if params.raise_session_caps is not None:
        raised = params.raise_session_caps.model_dump()
        for name, value in raised.items():
            if value < caps.get(name, 0):
                raise HTTPException(422, f"{name} 只能提高，不能借追问降低既有上限")
        scope_notes.append("会话上限将被显式提高：%s → %s（累计账本未重置）"
                           % (json.dumps(caps, ensure_ascii=False), json.dumps(raised, ensure_ascii=False)))
        caps = raised
    for kind, used, cap, requested in (
            ("模型调用", ledger["model_calls"], caps["max_session_model_calls"], budgets.max_model_calls),
            ("工具调用", ledger["tool_calls"], caps["max_session_tool_calls"], budgets.max_tool_calls)):
        if used >= cap:
            raise HTTPException(409, f"会话累计{kind}已达上限 {used}/{cap}；继续需要在本轮请求里显式 "
                                     "raise_session_caps，新建任务不会绕过这个上限")
        if used + (requested or 0) > cap:
            scope_notes.append(f"本轮预算会使会话累计{kind}超过上限 {cap}；到达上限时本轮会停止，"
                               "而不是继续花费")
    return ledger, caps


def _parent(store, rid: str) -> dict:
    run = store.get(rid, internal=True)
    if not run.get("conversation_id"):
        # Only reachable on a database written before the v2 migration ran.
        raise HTTPException(409, "这个任务没有会话关联；请先启动一次服务完成迁移，再追问")
    if run["status"] in OPEN_STATUSES:
        raise HTTPException(409, "上一轮仍在运行；等它结束或先取消，不能并发追问同一个会话")
    return run


def validate_followup(store, params, *, vault_public: dict, budgets) -> tuple[Scope, list[dict]]:
    """Decide everything about a follow-up turn before it is created.

    Order matters: the parent must be stopped, the reuse must be in scope, the destination must be
    agreed, and the ledger must have room — and only then does a run exist at all. A refusal leaves
    no half-created turn behind.
    """
    parent = _parent(store, params.parent_run)
    conversation_id = parent["conversation_id"]
    idempotency_key = (params.idempotency_key or "").strip()
    existing = store.run_by_key(conversation_id, idempotency_key)
    if existing:
        # A repeated submit is not a second turn. Returning the run it already created is what makes
        # a double click, a retried POST and a flaky network cost nothing.
        return Scope(conversation_id=conversation_id, parent_run=params.parent_run,
                     parent_turn=parent["turn"], turn=0, kind="followup", goal=params.goal,
                     authorizations={"idempotent_replay": True, "idempotency_key": idempotency_key},
                     notes=["命中同一个 idempotency_key，返回已创建的那一轮，没有新建任务"]), []
    notes: list[str] = []
    reuse, seeds, workspace_notes = collect_reuse(store, params, conversation_id)
    notes.extend(workspace_notes)
    notes.extend(check_destination(parent["config"], vault_public, params.trust_new_destination))
    ledger, caps = check_ledger(store, conversation_id, budgets, params, notes)
    use_library = bool(params.use_library)
    inherited = bool((parent["params"] or {}).get("use_library"))
    if use_library and not inherited:
        notes.append("本地文献库授权在本轮重新给出；上一轮没有这项授权，历史材料里没有库内元数据")
    if not use_library and inherited:
        notes.append("上一轮授权过本地文献库，本轮没有；库内元数据不会在本轮发送，已复用的历史材料原样保留")
    stale = [item for item in reuse if item.stale]
    if stale:
        notes.append("有 %d 条复用材料超过 %d 天；工具没有自动重抓，是否付费补查由你决定"
                     % (len(stale), STALE_AFTER_DAYS))
    authorizations = {
        "use_library": use_library,
        "library_reauthorized": use_library and not inherited,
        "spend": True,
        "consent_to_send": True,
        "trust_new_destination": bool(params.trust_new_destination),
        "reuse_routes": sorted({item.origin.get("route", "") for item in reuse}),
        "reuse_count": len(reuse),
        "workspace": params.workspace or "",
        "raise_session_caps": params.raise_session_caps.model_dump() if params.raise_session_caps else None,
        "idempotency_key": idempotency_key,
    }
    return Scope(conversation_id=conversation_id, parent_run=parent["id"], parent_turn=parent["turn"],
                 turn=store.next_turn(conversation_id), kind="followup", goal=params.goal,
                 reuse=reuse, authorizations=authorizations, caps=caps, ledger=ledger,
                 notes=notes), seeds


def validate_retry(store, params, *, vault_public: dict, budgets) -> tuple[Scope, list[dict]]:
    """A retry repeats the parent's goal and reuses nothing new.

    Deliberately narrower than a follow-up: no goal field and no reuse fields, so the only thing a
    retry can change is the budget. That is what keeps "try again" from becoming a way to widen scope
    without naming it.
    """
    parent = _parent(store, params.parent_run)
    if parent["status"] not in RETRYABLE_STATUSES:
        raise HTTPException(409, f"状态为 {parent['status']} 的任务不能重试；"
                                 f"可重试的是 {'、'.join(RETRYABLE_STATUSES)}，"
                                 "已完成的任务要补充条件请用追问")
    conversation_id = parent["conversation_id"]
    idempotency_key = (params.idempotency_key or "").strip() or \
        f"retry:{parent['id']}:{store.next_turn(conversation_id)}"
    existing = store.run_by_key(conversation_id, idempotency_key)
    if existing:
        return Scope(conversation_id=conversation_id, parent_run=parent["id"],
                     parent_turn=parent["turn"], turn=0, kind="retry", goal=parent["goal"],
                     authorizations={"idempotent_replay": True, "idempotency_key": idempotency_key},
                     notes=["命中同一个 idempotency_key，返回已创建的那一轮，没有新建任务"]), []
    notes: list[str] = ["重试沿用上一轮的目标原文，不新增范围；需要补充条件请用追问"]
    notes.extend(check_destination(parent["config"], vault_public, params.trust_new_destination))
    ledger, caps = check_ledger(store, conversation_id, budgets, params, notes)
    authorizations = {"use_library": bool((parent["params"] or {}).get("use_library")),
                      "library_reauthorized": False, "spend": True, "consent_to_send": True,
                      "trust_new_destination": bool(params.trust_new_destination),
                      "reuse_routes": [], "reuse_count": 0, "workspace": "",
                      "raise_session_caps": params.raise_session_caps.model_dump()
                      if params.raise_session_caps else None,
                      "idempotency_key": idempotency_key,
                      "library_inherited_note": "重试沿用上一轮的本地库授权，因为目标与发送范围都没有变"}
    return Scope(conversation_id=conversation_id, parent_run=parent["id"],
                 parent_turn=parent["turn"], turn=store.next_turn(conversation_id), kind="retry",
                 goal=parent["goal"], authorizations=authorizations, caps=caps, ledger=ledger,
                 notes=notes), []


def parent_report_text(report: dict | None, limit: int = 4000) -> str:
    """The previous turn's conclusions, bounded.

    A continuing turn is handed the earlier report rather than the earlier transcript: the transcript
    grows with every call and re-sends its own excerpts, while the report is the part that was already
    judged worth keeping. Replaying history would be a second memory system, and this is not one.

    The earlier report's evidence ids are deliberately *not* printed. They belong to the earlier run,
    and a report citing one is rejected — so showing them would only invite a citation this turn
    cannot make. Material this turn may cite is listed below, with ids of its own.
    """
    if not report:
        return "上一轮没有产出结构化报告，所以没有可继承的结论；本轮需要自己取证。"
    lines = [f"上一轮报告：{report.get('title', '')}",
             f"结论状态：{report.get('outcome', '')}",
             f"摘要：{str(report.get('summary', ''))[:1200]}"]
    for finding in (report.get("findings") or [])[:12]:
        lines.append("- [%s] %s" % (finding.get("assessment", "uncertain"),
                                    str(finding.get("claim", ""))[:CLAIM_CHARS]))
    for limitation in (report.get("limitations") or [])[:6]:
        lines.append(f"上一轮的限制：{str(limitation)[:300]}")
    lines.append("上一轮的报告没有被覆盖，仍可按其 run id 单独导出。本轮报告是新版本，"
                 "完成后会给出与上一轮的新增/改变/仍不确定对比。")
    lines.append("注意：上一轮报告里的证据 id 属于上一轮，本轮不能直接引用；"
                 "本轮可引用的材料在下面按新的 id 列出。")
    return "\n".join(lines)[:limit]


def handover_message(scope: Scope, *, parent_goal: str = "", parent_report: dict | None = None) -> str:
    """Everything a continuing turn is told about the work before it, in one bounded message."""
    head = [f"本轮是第 {scope.turn} 轮（{scope.kind}），父任务 {scope.parent_run} 是第 {scope.parent_turn} 轮。"]
    if parent_goal:
        head.append(f"上一轮目标：{parent_goal[:1200]}")
    head.append(f"本轮目标：{scope.goal}")
    head.append("")
    if scope.kind == "retry":
        head.append("这是一次重试：目标与上一轮完全相同，没有新增范围。上一轮没有完成，"
                    "请按同样的目标重新取证；不要假设上一轮已经拿到过什么。")
        head.append("")
    body = [parent_report_text(parent_report) if scope.kind == "followup" else "",
            reuse_message(scope)]
    return "\n".join([*head, *[part for part in body if part]])


def reuse_message(scope: Scope) -> str:
    """The bounded text a continuing turn receives about the history it may build on.

    Excerpts only, and a total cap: these lines are re-sent on every model call of the turn, so their
    size multiplies by the number of calls. Bodies stay in the evidence store and come back through
    `read_evidence`, which is the same arrangement the first turn uses for tool results.
    """
    lines: list[str] = []
    if not scope.reuse:
        lines.append("本轮没有复用历史材料：需要证据就调用检索工具，不要凭上一轮的记忆下结论。")
        lines.extend(f"作用域说明：{note}" for note in scope.notes)
        return "\n".join(lines)
    lines.append(f"以下 {len(scope.reuse)} 条材料已按授权复用，本轮没有重新抓取。"
                 "需要完整正文时用 read_evidence 按 id 取回：")
    budget = REUSE_EXCERPT_TOTAL
    for item in scope.reuse:
        age = f"{item.age:.0f} 天前" if item.age is not None else "来源时间未知"
        flag = "（已超过 %d 天阈值：工具没有自动重抓，是否付费补查由用户决定）" % STALE_AFTER_DAYS \
            if item.stale else ""
        head = f"- {item.evidence_id} ｜ 第 {item.origin.get('turn', '?')} 轮 ｜ {age}{flag} ｜ {item.summary}"
        keep = min(len(item.excerpt), max(0, budget))
        budget -= keep
        if keep:
            head += f"\n  摘录：{item.excerpt[:keep]}"
        if keep < len(item.excerpt):
            head += "（已截断，用 read_evidence 取回）"
        lines.append(head)
    lines.append("")
    lines.append("复用不等于人工验证：这些是当时工具返回的观察，引用时照旧给出 evidence id，"
                 "并说明它来自更早的轮次。上一轮的报告没有被覆盖。")
    for note in scope.notes:
        lines.append(f"作用域说明：{note}")
    return "\n".join(lines)


def _finding_key(finding: dict, id_map: dict | None = None) -> tuple:
    """Findings are matched across turns by the evidence they rest on, not by their wording.

    A reworded claim over the same evidence is the same finding changed; a claim over new evidence is
    a new one. Matching on text would report every rephrasing as an addition and hide the case that
    matters — a conclusion that moved.

    `id_map` translates a reused evidence id back to the id it came from. Reuse mints a fresh id in
    the new turn (evidence ids are per-run), so without the map a finding carried forward unchanged
    would be reported as one addition and one drop — which reads as churn where nothing moved.
    """
    mapping = id_map or {}
    return tuple(sorted(mapping.get(eid, eid) for eid in finding.get("evidence_ids") or []))


def _brief(finding: dict) -> dict:
    return {"claim": str(finding.get("claim") or "")[:CLAIM_CHARS],
            "assessment": finding.get("assessment", "uncertain"),
            "evidence_ids": list(finding.get("evidence_ids") or [])}


def report_delta(previous: dict | None, report: dict, *, id_map: dict | None = None) -> dict:
    """What this turn's report adds, changes, drops or still leaves open.

    `dropped` means only that this turn did not repeat the claim. It does not mean the earlier
    conclusion was overturned, and it does not delete it: the earlier report stays exportable under
    its own run id.
    """
    current = {_finding_key(finding, id_map): finding for finding in report.get("findings") or []}
    if not previous:
        return {"against_run": "", "against_turn": 0, "added": [_brief(f) for f in current.values()],
                "changed": [], "dropped": [], "resolved_from_uncertain": [],
                "still_uncertain": [_brief(f) for f in current.values()
                                    if f.get("assessment") == "uncertain"],
                "outcome": {"previous": "", "current": report.get("outcome", "")},
                "note": "本会话此前没有已完成的报告，所以这一轮的内容全部计为新增。"}
    before = {_finding_key(finding): finding for finding in previous["report"].get("findings") or []}
    added = [_brief(current[key]) for key in current if key not in before]
    changed, resolved = [], []
    for key, finding in current.items():
        if key not in before:
            continue
        earlier = before[key]
        if earlier.get("assessment") == "uncertain" and finding.get("assessment") != "uncertain":
            resolved.append({"from": _brief(earlier), "to": _brief(finding)})
        if str(earlier.get("claim", "")).strip() != str(finding.get("claim", "")).strip() \
                or earlier.get("assessment") != finding.get("assessment"):
            changed.append({"from": _brief(earlier), "to": _brief(finding)})
    dropped = [_brief(before[key]) for key in before if key not in current]
    return {"against_run": previous["run_id"], "against_turn": previous["turn"],
            "added": added, "changed": changed, "dropped": dropped,
            "resolved_from_uncertain": resolved,
            "still_uncertain": [_brief(f) for f in current.values() if f.get("assessment") == "uncertain"],
            "outcome": {"previous": previous["report"].get("outcome", ""),
                        "current": report.get("outcome", "")},
            "note": "与上一轮相比的变化。上一轮报告没有被覆盖，仍可按它的 run id 单独导出；"
                    "dropped 只表示本轮没有再提，不表示上一轮结论被推翻。"}


def turn_snapshot(scope: Scope, *, config_public: dict, budgets, allowed_tools: list[str]) -> dict:
    """The immutable record of what authorized this turn.

    Written once, at creation, and never updated by a checkpoint: a later edit to a budget or a
    permission must not retroactively explain a turn that already ran.
    """
    return {"schema_version": "1", "kind": scope.kind, "conversation_id": scope.conversation_id,
            "turn": scope.turn, "parent_run": scope.parent_run, "parent_turn": scope.parent_turn,
            "goal": scope.goal, "authorized_at": now(),
            "model_destination": {name: config_public.get(name, "") for name in DESTINATION_FIELDS},
            "permissions": {**scope.authorizations, "allowed_tools": allowed_tools},
            "budgets": budgets.model_dump(),
            "ledger_before_turn": scope.ledger, "caps": scope.caps,
            "reuse": [item.as_dict() for item in scope.reuse],
            "scope_notes": scope.notes}
