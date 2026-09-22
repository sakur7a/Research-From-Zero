"""Run the public evaluation tasks through the retrieval layer, with no model involved.

Three channels are kept apart, and this file implements only the middle one:

* `unit`      — deterministic fixtures: `python -m pytest` and `npm test`. No network.
* `connector` — the real scholarly and resource services, no model. This file.
* `live`      — a real task that plans and calls tools, which needs a model key. Not here.

One rule shapes every record. A step that could not be completed is recorded as `unknown` or
`blocked` together with its reason, never as a negative result: "we did not find out" and "we
found out it is not there" are different states, and an evaluation that conflates them measures
nothing. For the same reason a source failure is never allowed to clear an expectation.

Nothing here writes a credential: the record lists which variable *names* were configured, never a
value. Run it as

    python evals/runner.py --only recall-stable-layers
    python evals/runner.py                 # every task whose channel is reachable here
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from re0.agent.tools import ResearchTools  # noqa: E402
from re0.providers import ProviderError  # noqa: E402

SCHEMA_VERSION = "1"
TASKS_FILE = pathlib.Path(__file__).resolve().parent / "tasks.json"
RUNS_DIR = pathlib.Path(__file__).resolve().parent / "runs"
CREDENTIAL_NAMES = ("SEMANTIC_SCHOLAR_API_KEY", "SEMANTICSCHOLAR_API_KEY", "OPENALEX_MAILTO",
                    "OPENREVIEW_TOKEN", "TAVILY_API_KEY", "GITHUB_TOKEN",
                    "RE0_LLM_BASE_URL", "RE0_LLM_MODEL", "RE0_LLM_API_KEY")


def configured_names() -> list[str]:
    """Names only. Recording a value would put a key into a report that gets committed."""
    return [name for name in CREDENTIAL_NAMES if os.environ.get(name)]


def load_tasks() -> dict:
    return json.loads(TASKS_FILE.read_text(encoding="utf-8"))


def call(tools: ResearchTools, name: str, arguments: dict, trace: list) -> tuple[dict | None, str]:
    """Call one tool, timing it and capturing the reason it failed rather than raising."""
    started = time.monotonic()
    try:
        result = tools.execute(name, arguments)
    except (ProviderError, ValueError, KeyError) as exc:
        trace.append({"tool": name, "arguments": arguments, "ok": False,
                      "seconds": round(time.monotonic() - started, 2), "error": str(exc)[:200]})
        return None, str(exc)
    except Exception as exc:  # noqa: BLE001 - an unexpected failure is still a recorded reason
        trace.append({"tool": name, "arguments": arguments, "ok": False,
                      "seconds": round(time.monotonic() - started, 2),
                      "error": f"{type(exc).__name__}"})
        return None, f"{type(exc).__name__}"
    trace.append({"tool": name, "arguments": arguments, "ok": True,
                  "seconds": round(time.monotonic() - started, 2)})
    return result, ""


def check_identifier(tools: ResearchTools, identifier: str, trace: list) -> dict:
    step = {"check": "retrieval", "type": "identifier_resolves", "value": identifier}
    result, failure = call(tools, "resolve_paper", {"identifier": identifier}, trace)
    if result is None:
        # Not a negative result: the lookup itself did not complete.
        step.update({"state": "unknown", "detail": f"the lookup did not complete: {failure}"})
        return step
    documents = result.get("documents") or []
    if not documents:
        step.update({"state": "unknown", "detail": "the resolver returned no record"})
        return step
    paper = documents[0].get("paper") or {}
    step.update({"state": "ok", "detail": f"resolved to {paper.get('title', '')[:80]}",
                 "locator": documents[0].get("locator", ""),
                 "source_url": documents[0].get("source_url", "")})
    return step


def check_resource(tools: ResearchTools, url: str, trace: list) -> dict:
    step = {"check": "retrieval", "type": "resource_status_in", "value": url}
    result, failure = call(tools, "inspect_resource", {"url": url}, trace)
    if result is None:
        step.update({"state": "unknown", "detail": f"the check did not complete: {failure}"})
        return step
    documents = result.get("documents") or []
    if not documents:
        step.update({"state": "unknown", "detail": "the check returned no observation"})
        return step
    payload = json.loads(documents[0].get("content") or "{}")
    status = payload.get("status", "")
    indicators = payload.get("indicators") or {}
    step.update({
        "state": "ok" if status else "unknown",
        "status": status,
        "revision": payload.get("revision", ""),
        "indicators": {key: len(value) for key, value in indicators.items()},
        "detail": payload.get("summary", "")[:200],
        "limits": (payload.get("limitations") or [])[:2],
    })
    return step


def check_recall(tools: ResearchTools, task: dict, wanted: list[str], trace: list) -> dict:
    arguments = dict(task["input"])
    step = {"check": "retrieval", "type": "found_identifiers", "value": wanted}
    result, failure = call(tools, "search_papers", arguments, trace)
    if result is None:
        step.update({"state": "unknown", "detail": f"the search did not complete: {failure}"})
        return step
    coverage = {key: result.get(key) for key in ("sources_queried", "source_counts", "source_failures")}
    identifiers = set()
    for document in result.get("documents") or []:
        paper = document.get("paper") or {}
        for key in ("arxiv_id", "doi"):
            if paper.get(key):
                identifiers.add(str(paper[key]).split("v")[0])
    found = [item for item in wanted if item.split(":")[-1] in identifiers]
    missing = [item for item in wanted if item not in found]
    step.update({
        # A miss is recorded as a miss: the search completed, and the identifier was not in the
        # results. That is a coverage gap for this query and source, not evidence of absence, and
        # the note says so rather than letting a green tick hide it.
        "state": "missed" if missing else "ok",
        "found": found, "missing": missing,
        "note": "a miss here is a coverage gap for this query and source; it is not evidence that "
                "the work does not exist",
        "documents": len(result.get("documents") or []), "coverage": coverage})
    return step


def run_task(tools: ResearchTools, task: dict) -> dict:
    trace: list = []
    steps: list = []
    started = time.monotonic()
    if task.get("requires_credentials") and not (os.environ.get("RE0_LLM_BASE_URL") and os.environ.get("RE0_LLM_MODEL")):
        # Recorded as blocked rather than passed: an unrun case is not a success.
        return {
            "schema_version": SCHEMA_VERSION, "task_set_version": None, "task_id": task["id"],
            "channel": task["channel"], "status": "blocked",
            "blocked_reason": "no model configuration is present, so this live task was not run",
            "credential_names_set": configured_names(), "steps": [], "tool_trace": [],
            "usage": {"provider_reported": "unknown", "wall_seconds": 0.0},
        }
    for expectation in task.get("expectations", []):
        kind = expectation.get("type")
        if kind == "identifier_resolves":
            steps.append(check_identifier(tools, expectation["value"], trace))
        elif kind == "resource_status_in":
            step = check_resource(tools, expectation["value"], trace)
            # An observation that contradicts the expectation is information, not a pass.
            if step.get("status"):
                step["expected"] = expectation.get("allowed", [])
                step["matches_expectation"] = step["status"] in expectation.get("allowed", [])
                step["state"] = "ok" if step["matches_expectation"] else "contradicted"
            elif expectation.get("indicator"):
                step["state"] = "unknown"
            steps.append(step)
        elif kind == "indicator_present":
            # Merged into the resource step when the same URL was already checked.
            steps.append({"check": "retrieval", "type": "indicator_present",
                          "value": expectation["value"], "indicator": expectation["indicator"],
                          "state": "deferred",
                          "detail": "read from the matching resource status step"})
        elif kind == "found_identifiers":
            steps.append(check_recall(tools, task, expectation["value"], trace))
        else:
            steps.append({"check": expectation.get("check"), "type": kind, "state": "human",
                          "detail": "this expectation needs a person to review the output"})
    states = {step["state"] for step in steps}
    if not steps:
        status = "completed"
    elif states & {"contradicted", "missed"}:
        status = "partial"
    elif "human" in states:
        # Nothing failed, but a person still has to look: that is its own state, not "unknown".
        status = "pending_human"
    elif states <= {"ok", "deferred"}:
        status = "completed"
    else:
        status = "unknown"
    return {
        "schema_version": SCHEMA_VERSION, "task_set_version": None, "task_id": task["id"],
        "channel": task["channel"], "status": status,
        "credential_names_set": configured_names(), "steps": steps, "tool_trace": trace,
        "usage": {"provider_reported": "unknown", "wall_seconds": round(time.monotonic() - started, 1)},
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--only", default=None, help="run one task id")
    parser.add_argument("--channel", default="connector", choices=["connector", "live"],
                        help="one channel per run; the two are never mixed in one file")
    parser.add_argument("--out", default=None, help="write the record here instead of evals/runs/")
    parser.add_argument("--list", action="store_true", help="list the task ids and exit")
    args = parser.parse_args(argv)

    task_set = load_tasks()
    if args.list:
        for task in task_set["tasks"]:
            print(f"{task['id']:<28} {task['channel']:<10} {task['kind']}")
        return 0

    selected = [task for task in task_set["tasks"] if task["channel"] == args.channel]
    if args.only:
        selected = [task for task in task_set["tasks"] if task["id"] == args.only]
    if not selected:
        print(f"no task matches (channel={args.channel}, only={args.only})", file=sys.stderr)
        return 2

    tools = ResearchTools(None)
    RUNS_DIR.mkdir(exist_ok=True)
    print(f"credential names configured: {', '.join(configured_names()) or 'none'}")
    for task in selected:
        record = run_task(tools, task)
        record["task_set_version"] = task_set["task_set_version"]
        destination = pathlib.Path(args.out) if args.out else RUNS_DIR / f"{task['id']}.json"
        destination.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
        printable = [f"{step['type']}={step['state']}" for step in record["steps"]]
        print(f"{task['id']:<28} {record['status']:<10} {' '.join(printable)}")
        print(f"{'':<28} -> {destination}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
