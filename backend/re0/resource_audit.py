"""Candidate discovery and field-level resource audit, returned as data.

`skill_search` used to discover candidates, verify them and print them inside one function. Two
things were lost on the way out, because a print statement is not a record: a name search whose
endpoints failed was reported to the terminal and never attached to the document, and a `--verify`
result was printed and never entered `--json`. This module is the other half of that split — it
fetches and decides, the printers only render.

Two rules it holds:

* **A check describes the check.** `access_failed` and `not_found_in_scope` say what this run could
  see. Neither is a claim that the authors released nothing, and a 404 is kept apart from a 429 by
  carrying the provider's own status beside ours.
* **A name match stays a candidate.** Nothing here promotes one to an official attribution; that is
  a human revision with a source attached, and the model refuses to record it without one.
"""
from __future__ import annotations

import json
import re
from posixpath import basename

from .literature import artifact_urls
from .models import (AUDIT_COMPONENTS, AUDIT_STATES, ComponentFinding, Evidence, ResourceAudit,
                     VERIFICATION_DEPTHS, now)
from .providers import ProviderError, check_resource, repository_identity

PROJECT_NAME_PATTERN = re.compile(r"[A-Za-z][A-Za-z0-9]*(?:[-_.][A-Za-z0-9]+)*")
NAME_ENDPOINTS = (("search_repositories", {}, "GitHub 名称检索"),
                  ("search_hub", {"kind": "models"}, "HF models 名称检索"),
                  ("search_hub", {"kind": "datasets"}, "HF datasets 名称检索"))
DISCOVERY_ATTEMPTS = 2
PER_ENDPOINT_LIMIT = 3
# Two kinds of "the check did not cover everything", and only the first changes an answer.
# A listing that was cut means an absent file may exist, so absences become unknowns. A README that
# 404s says nothing about which files the repository holds, so it stays a limitation and nothing
# more — treating it as incompleteness would downgrade every repository without a README.
LISTING_INCOMPLETE = ("truncated=true", "未返回文件清单")
CHECK_INCOMPLETE = ("Release 检查未完成",)
ADAPTER_PATTERN = re.compile(r"(adapter|lora|qlora|peft)", re.I)
# Declared links outrank every name match: an author pointing at a repository is a claim, a
# search hit is a coincidence until something cross-checks it.
DECLARED_RANK = 4
COMPONENT_FROM_INDICATOR = (("training", "code_training"), ("inference", "code_inference"),
                            ("evaluation", "code_evaluation"), ("weights", "checkpoint"),
                            ("data", "dataset"), ("split", "data_split"),
                            ("preprocessing", "preprocessing"), ("environment", "environment"))
SEARCH_STATES = ("searched", "partial", "failed", "skipped", "not-run")
# What the provider's own status maps to. Anything unlisted and unverified is a failed check,
# never an absent resource. An empty repository is the one "nothing here" a provider states
# positively, so it maps to the scope statement rather than to a failure.
STATE_FROM_PROVIDER = {"gated": "access_required", "unsupported": "unsupported",
                       "empty_repository": "not_found_in_scope"}
# A check that did not complete leaves `access` unknown: it is absent from this mapping on purpose,
# because "failed" would be a second guess about a resource nobody reached.
ACCESS_FROM_STATE = {"metadata_readable": "open", "partially_available": "open",
                     "access_required": "requires_application",
                     "not_found_in_scope": "not_applicable"}


def short_name(title: str) -> str:
    """The project name a paper titles itself with: the part before a colon or dash.

    "RevealLayer: Disentangling Hidden and Visible Layers…" -> "RevealLayer", which is also the
    repository name. A sentence-like head is rejected rather than searched, because searching a
    sentence returns noise.
    """
    head = re.split(r"[:\u2014\u2013]|\s-\s", title, maxsplit=1)[0].strip().strip('"\'\u201c\u201d')
    if PROJECT_NAME_PATTERN.fullmatch(head) and 3 <= len(head) <= 40:
        return head
    return ""


def _normalise(value: str) -> str:
    return re.sub(r"[-_.]", "", (value or "").lower())


def candidate_origin(label: str, content: str, title: str, name: str) -> tuple:
    """Rank a name match by how much evidence it carries, because a fuzzy search returns noise.

    A description that repeats the paper title is the strongest cheap signal of ownership, an
    identifier whose last segment equals the project name comes next, and a shared word alone is
    the weakest — `Stability-AI/Stable-Layers` and `nathannlu/aperture` can both come back from
    one query, and the reader needs to see which is which. None of it proves authorship.
    """
    body = content or ""
    try:
        payload = json.loads(body)
    except ValueError:
        payload = {}
    head = " ".join(title.split()[:4]).lower()
    if head and head in body.lower():
        return label + "·描述与论文标题相符", 3
    identifier = str(payload.get("full_name") or payload.get("id") or "")
    if _normalise(identifier.split("/")[-1]) == _normalise(name):
        return label + "·标识名与项目名一致", 2
    return label + "·仅名称匹配", 1


def discover(document: dict, tools, *, per_kind: int = PER_ENDPOINT_LIMIT, not_run: str = "") -> dict:
    """Find one paper's resource candidates. Fetches, decides, prints nothing.

    `not_run` names why discovery was not attempted at all — `disabled` or `budget` — so that a
    paper nobody searched is distinguishable from one somebody searched and found nothing for.
    """
    paper = document.get("paper") or {}
    title = paper.get("title", "")
    abstract = paper.get("abstract", "") or ""
    candidates = [{"url": url, "origin": "摘要中自述", "rank": DECLARED_RANK, "declared": True}
                  for url in artifact_urls(abstract)]
    name = short_name(title)
    attempts, failures, reached = [], [], 0
    if tools is None or not_run:
        state, reason = "not-run", (not_run or "disabled")
    elif not name:
        state, reason = "skipped", "标题里没有可检索的项目名（形如「Name: ...」）"
    else:
        reason = ""
        for tool, extra, label in NAME_ENDPOINTS:
            result, last = None, None
            for _ in range(DISCOVERY_ATTEMPTS):
                try:
                    result = tools.execute(tool, {"query": name, "limit": per_kind, **extra})
                    break
                except (ProviderError, ValueError) as exc:
                    last = exc
            if result is None:
                # Kept per endpoint: "GitHub answered, the Hub did not" is a different situation
                # from "nothing answered", and only the first is a partial result.
                failures.append(f"{label}: {last}")
                attempts.append({"endpoint": label, "ok": False, "error": str(last), "candidates": 0})
                continue
            hits = [item for item in (result.get("documents") or []) if isinstance(item, dict)]
            reached += 1
            attempts.append({"endpoint": label, "ok": True, "candidates": len(hits)})
            for item in hits:
                origin, rank = candidate_origin(label, item.get("content", ""), title, name)
                candidates.append({"url": item.get("source_url", ""), "origin": origin,
                                   "rank": rank, "declared": False})
        state = "searched" if reached == len(NAME_ENDPOINTS) else ("partial" if reached else "failed")
    candidates = [item for item in candidates if item["url"]]
    # Strongest evidence first, so a fuzzy hit cannot sit above the likely official repository.
    candidates.sort(key=lambda entry: -entry["rank"])
    return {"project_name": name, "state": state, "reason": reason, "candidates": candidates,
            "attempts": attempts, "failures": failures}


def _paper_link(paper: dict) -> str:
    """The work's own address, which is where a declaration in its abstract can be checked."""
    if paper.get("arxiv_id"):
        return f"https://arxiv.org/abs/{paper['arxiv_id']}"
    if paper.get("doi"):
        return f"https://doi.org/{paper['doi']}"
    return str(paper.get("paper_url") or "")


def _work_version(paper: dict, publication: dict) -> str:
    identifier = str(paper.get("arxiv_id") or "")
    if re.search(r"v\d+$", identifier):
        return f"arXiv {identifier}"
    # A venue names a version of the work. "arXiv (Cornell University)" names the indexer that
    # reported the record, which is not a version and would be read as one.
    if publication.get("state") not in {"", None, "preprint"} and publication.get("venue"):
        return str(publication["venue"])[:200]
    return ""


def _declaring_sentence(abstract: str, url: str) -> str:
    for sentence in re.split(r"(?<=[.!?。；;])\s+", abstract or ""):
        if url in sentence:
            return sentence[:600]
    return (abstract or "")[:600]


def _resource_type(url: str) -> tuple:
    """`(type, provider, category, identity)`. An unsupported link is still a row, typed unknown."""
    try:
        provider, category, identity = repository_identity(url)
    except (ProviderError, ValueError):
        return "unknown", "", "", ""
    kind = "code" if provider == "github" else ("dataset" if category == "datasets" else "checkpoint")
    return kind, provider, category, identity


def adapter_weights(observation) -> list:
    """Weight files whose names mark them as adapter/LoRA deltas.

    An adapter is not a checkpoint a reader can run: it needs the base model it was trained
    against, which the filename does not name. Reporting it as simply present would hide that.
    """
    return [path for path in (observation.indicators or {}).get("weights", [])
            if ADAPTER_PATTERN.search(basename(path))]


def component_coverage(observation, status: str, depth: str, incomplete: bool) -> dict:
    """Per-artifact-class coverage. `not_applicable` and `unknown` stay different answers.

    Every class carries the source it rests on: a hit carries the file, and an absence carries the
    listing that was searched, because "not in this listing" is only checkable next to the listing.
    A conclusion that cannot point anywhere is downgraded to `unknown` rather than asserted.
    """
    listing = next((item.source_url for item in observation.evidence
                    if item.category == "provider_metadata"), "")
    by_path = {item.locator: item.source_url for item in observation.evidence
               if item.category == "file_name_candidate"}
    if depth == "not_checked":
        # A check that did not complete says nothing about any class, including the ones a
        # reader most wants an answer for.
        state = "check_failed" if status == "access_failed" else "unknown"
        return {name: ComponentFinding(state=state) for name in AUDIT_COMPONENTS}
    if status == "access_required" and depth == "metadata_only":
        return {name: ComponentFinding(state="requires_access", sources=[listing] if listing else [])
                for name in AUDIT_COMPONENTS}
    coverage = {}
    for indicator, component in COMPONENT_FROM_INDICATOR:
        paths = ((observation.indicators or {}).get(indicator) or [])[:4]
        sources = ([by_path[path] for path in paths if path in by_path] or ([listing] if listing else []))[:4]
        if paths and sources:
            coverage[component] = ComponentFinding(state="present", paths=paths, sources=sources)
        elif incomplete or not sources:
            # A truncated listing cannot support "not there"; the provider said so itself.
            coverage[component] = ComponentFinding(state="unknown", paths=paths,
                                                   sources=[listing] if listing else [])
        else:
            coverage[component] = ComponentFinding(state="absent_in_scope", sources=sources)
    return coverage


def licences_declared(observation, provider: str, category: str) -> dict:
    """One licence claim per artifact class, each stored as the declaration it came from.

    This records what a source said. It is not a legal conclusion, and a repository licence is not
    automatically a licence for the weights or the data inside it.
    """
    declared = (observation.license_id or "").strip()
    if not declared:
        return {}
    if provider == "github":
        return {"code": declared}
    return {"dataset": declared} if category == "datasets" else {"checkpoint": declared}


def _declaration_evidence(url: str, candidate: dict, paper: dict) -> list:
    """A link the authors put in their own abstract, recorded as the declaration it is.

    The abstract is a source a reader can open, so the declaration carries its sentence rather
    than a bare flag — otherwise `released` would be an assertion with nothing behind it.
    """
    if not candidate.get("declared"):
        return []
    return [Evidence(source_url=_paper_link(paper) or url, locator="论文摘要",
                     excerpt=_declaring_sentence(paper.get("abstract", ""), url),
                     category="author_declaration")]


def bounded_evidence(observation, limit: int = 6) -> list:
    """The check's own evidence, trimmed. Release assets and README links live only here, so a row
    that dropped them would lose the one place a checkpoint is sometimes published."""
    return [Evidence(source_url=item.source_url, locator=item.locator[:400],
                     excerpt=item.excerpt[:400], category=item.category)
            for item in observation.evidence[:limit]]


def audit_from_observation(url: str, observation, *, candidate: dict, paper: dict,
                           publication: dict) -> ResourceAudit:
    """Turn one bounded check into an audit row. The observation is the source; nothing is added
    that it did not establish."""
    provider_status = observation.status
    # The HTTP code is the fact; `status` is our reading of it. A 404 and a 429 both read as a
    # failed check, and only the code tells a reader which one is worth retrying.
    reported_status = f"HTTP {observation.http_status}" if observation.http_status else provider_status
    depth = "not_checked" if observation.depth == "not_verified" else observation.depth
    limitations_text = list(observation.limitations)
    listing_incomplete = any(marker in text for text in limitations_text for marker in LISTING_INCOMPLETE)
    check_incomplete = any(marker in text for text in limitations_text for marker in CHECK_INCOMPLETE)
    adapters = adapter_weights(observation)
    if depth == "not_checked":
        status = STATE_FROM_PROVIDER.get(provider_status, "access_failed")
    elif provider_status in STATE_FROM_PROVIDER:
        status = STATE_FROM_PROVIDER[provider_status]
    elif listing_incomplete or check_incomplete or adapters:
        status = "partially_available"
    else:
        status = "metadata_readable"
    kind, provider, category, _ = _resource_type(url)
    coverage = component_coverage(observation, status, depth, listing_incomplete)
    # Findings about *this* resource come before the standing caveats about the method, because a
    # renderer that shows only the first few limitations must show the ones that differ per row.
    findings = []
    if adapters:
        findings.append("检测到 adapter/LoRA 形式的权重候选（" + "、".join(adapters[:3])
                        + "），通常需要对应的基础模型才能使用；本次未验证该对应关系。")
    if status == "access_failed":
        findings.append("本次检查没有完成；这不代表资源不存在或未开放。")
    if status in {"metadata_readable", "partially_available"} and \
            not any(finding.state == "present" for finding in coverage.values()):
        # An empty repository is a real finding, and it still is not a claim about anything outside
        # the scope that was actually listed.
        findings.append("检查范围内没有任何候选文件（包括只有 README 的空仓库）；"
                        "这描述的是本次检查范围，不是资源不存在。")
    findings.append("未建立论文版本与资源版本的对应关系；version_match=unknown 表示未判断，"
                    "不表示不匹配。")
    method = list(observation.limitations)
    if status in {"metadata_readable", "partially_available"}:
        method.append("本次只读取元数据与文件名，未下载文件字节、未运行代码；"
                      "「有候选」是文件名层面的结论，不是功能验证。")
    limitations = findings + method
    declarations = _declaration_evidence(url, candidate, paper)
    return ResourceAudit(
        paper_title=(paper.get("title") or url)[:600],
        work_identifier=str(paper.get("doi") or paper.get("arxiv_id") or "")[:300],
        work_version=_work_version(paper, publication),
        resource_url=url, resource_type=kind,
        candidate_origin=str(candidate.get("origin") or "")[:300],
        # Always unconfirmed here: promotion to official or third_party is a human revision that
        # has to bring its own cross-evidence, and a name match never carries one.
        attribution="unconfirmed",
        author_declaration="released" if declarations else "undeclared",
        author_declaration_evidence=declarations,
        status=status, provider_status=reported_status[:120], provider=observation.provider,
        summary=str(observation.summary)[:1000],
        access=ACCESS_FROM_STATE.get(status, "unknown"),
        scope=str(observation.scope)[:600], revision=observation.revision,
        checked_at=observation.checked_at or now(),
        verification_depth=depth if depth in VERIFICATION_DEPTHS else "metadata_only",
        coverage=coverage,
        evidence=bounded_evidence(observation),
        licences=licences_declared(observation, provider, category),
        limitations=[text[:1000] for text in limitations][:20],
    )


def unchecked_audit(url: str, *, candidate: dict, paper: dict, publication: dict,
                    reason: str, provider_status: str = "") -> ResourceAudit:
    """A candidate that was not checked — the budget ran out, or the check raised.

    It stays a row, because dropping it would make an unchecked link indistinguishable from one
    that was checked and found wanting. `provider_status` keeps those two apart in the data rather
    than only in prose.
    """
    kind, provider = _resource_type(url)[:2]
    declarations = _declaration_evidence(url, candidate, paper)
    return ResourceAudit(
        paper_title=(paper.get("title") or url)[:600],
        work_identifier=str(paper.get("doi") or paper.get("arxiv_id") or "")[:300],
        work_version=_work_version(paper, publication),
        resource_url=url, resource_type=kind,
        candidate_origin=str(candidate.get("origin") or "")[:300],
        author_declaration="released" if declarations else "undeclared",
        author_declaration_evidence=declarations,
        status="not_checked", provider_status=provider_status[:120], provider=provider,
        access="unknown", verification_depth="not_checked",
        coverage={name: ComponentFinding(state="unknown") for name in AUDIT_COMPONENTS},
        limitations=[f"本次未核验该候选：{reason}。未核验不是核验失败，也不是资源不可用。"],
    )


def audit_candidate(url: str, *, candidate: dict, paper: dict, publication: dict,
                    transport=None) -> ResourceAudit:
    """One bounded check as a row. Never raises: an exception becomes a row that says the check
    did not complete, because a traceback is not evidence about the resource."""
    try:
        observation = check_resource(url, transport)
    except Exception:
        observation = None
    if observation is None:
        return unchecked_audit(url, candidate=candidate, paper=paper, publication=publication,
                               reason="检查过程出现异常，未完成", provider_status="check_error")
    return audit_from_observation(url, observation, candidate=candidate, paper=paper,
                                  publication=publication)


def outcome(state: str, rows: list) -> str:
    """Roll a paper's audits up to one state, using the same vocabulary as a row.

    The leading checked candidate names the outcome; every other row is still in
    `resource_audits`, so the roll-up shortens a list without hiding an entry.

    "Checked" is `status != not_checked`, not a depth: a check that ran and failed reached no depth
    at all, and counting it as unchecked would report an attempted verification as one nobody tried.
    """
    verified = [row for row in rows if row["status"] != "not_checked"]
    if verified:
        return verified[0]["status"]
    if rows:
        return "candidate_located"
    if state == "searched":
        # The search ran to completion and returned nothing. That is a finding about the scope
        # searched, not about whether the authors released something.
        return "not_found_in_scope"
    if state in {"partial", "failed"}:
        return "access_failed"
    return "not_checked"


def attach(document: dict, discovery: dict) -> None:
    """Write one paper's discovery onto its document, so the JSON exit carries it.

    The failure list travels with the state rather than only beside it: `partial` with no reasons
    attached is a claim a reader cannot act on.
    """
    document["artifact_candidates"] = [{"url": item["url"], "origin": item["origin"]}
                                       for item in discovery["candidates"]]
    document["artifact_search"] = discovery["state"]
    document["artifact_search_detail"] = {
        "project_name": discovery["project_name"],
        "reason_not_run": discovery["reason"],
        "endpoints": discovery["attempts"],
        "failures": discovery["failures"],
    }


class Budget:
    """The two caps on a run, shared by every paper in it.

    Shared rather than per paper, so the first result cannot spend what a later one needs. What is
    left at the end is the run's own denominator, which is the difference between "we checked
    nothing" and "there was nothing to check".
    """

    def __init__(self, find: int = 0, verify: int = 0):
        self.find_total, self.verify_total = max(0, find), max(0, verify)
        self.find_left, self.verify_left = self.find_total, self.verify_total

    def take_find(self, wanted: bool) -> str:
        """Consume one name search, or say why it did not run.

        `wanted` is whether this paper has a project name to search for. A paper without one must
        not spend the budget: `--find-artifacts 10` means ten name searches, not ten papers looked
        at, and burning a slot on a survey title silently shortens the run.
        """
        if not wanted:
            return ""
        if self.find_total <= 0:
            return "disabled"
        if self.find_left <= 0:
            return "budget"
        self.find_left -= 1
        return ""

    def take_verify(self) -> bool:
        if self.verify_left <= 0:
            return False
        self.verify_left -= 1
        return True


def audit_document(document: dict, tools, budget: Budget, *, transport=None,
                   per_kind: int = PER_ENDPOINT_LIMIT) -> None:
    """Discover and verify for one paper, and attach both to the document. Fetches, prints nothing."""
    not_run = "disabled" if tools is None else \
        budget.take_find(bool(short_name((document.get("paper") or {}).get("title", ""))))
    discovery = discover(document, tools, per_kind=per_kind, not_run=not_run)
    attach(document, discovery)
    paper = document.get("paper") or {}
    publication = document.get("publication") or {}
    rows = []
    for candidate in discovery["candidates"]:
        if budget.take_verify():
            rows.append(audit_candidate(candidate["url"], candidate=candidate, paper=paper,
                                        publication=publication, transport=transport))
        else:
            rows.append(unchecked_audit(candidate["url"], candidate=candidate, paper=paper,
                                        publication=publication,
                                        reason="核验预算已用尽（--verify 可提高）"))
    document["resource_audits"] = [row.model_dump(mode="json") for row in rows]
    document["artifact_outcome"] = outcome(document["artifact_search"], document["resource_audits"])


def run_coverage(documents: list, budget: Budget) -> dict:
    """What the run covered, with its denominators.

    A count without a denominator reads as a result: "0 verified" is either "nothing to verify" or
    "the budget ran out", and only the second is a gap.
    """
    searches = {state: 0 for state in SEARCH_STATES}
    audits = {state: 0 for state in AUDIT_STATES}
    failures, declared, matched, verified = [], 0, 0, 0
    for document in documents:
        state = document.get("artifact_search", "not-run")
        searches[state] = searches.get(state, 0) + 1
        detail = document.get("artifact_search_detail") or {}
        title = (document.get("paper") or {}).get("title", "")[:80]
        failures.extend(f"[{title}] {item}" for item in detail.get("failures") or [])
        for row in document.get("resource_audits") or []:
            audits[row["status"]] = audits.get(row["status"], 0) + 1
            # Counted by state, not by depth: a check that ran and failed reached no depth, and
            # reporting it as "not verified" would hide an attempt that was actually made.
            verified += row["status"] != "not_checked"
            if "摘要中自述" in row.get("candidate_origin", ""):
                declared += 1
            else:
                matched += 1
    return {
        "papers": len(documents),
        "name_search": {"budget": budget.find_total, "spent": budget.find_total - budget.find_left,
                        "remaining": budget.find_left, "states": searches,
                        "denominator": len(documents),
                        "note": "states 的分母是本次结果里的论文数；skipped 表示标题没有可检索的项目名，"
                                "not-run 表示未开启或预算已尽，二者都不是「检索过且没有结果」。"},
        "candidates": {"found": declared + matched, "declared": declared, "name_matched": matched,
                       "verified": verified, "unchecked": declared + matched - verified,
                       "budget": budget.verify_total, "remaining": budget.verify_left},
        "audits": {state: audits.get(state, 0) for state in AUDIT_STATES},
        "failures": failures,
    }
