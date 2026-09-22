"""Score one channel's run records against the labelled task set.

Two rules from the issue are enforced here rather than left to prose:

* **No denominator is not 100%.** When no sample has been judged official, accuracy is reported as
  `null` with its reason: a rate over nothing has no value, and printing 1.0 for it would be the
  kind of number that makes a weak evaluation look finished.
* **Channels never mix.** `score()` takes one channel and reads only that channel's records, so a
  fixture result cannot reach a live metric and a live result cannot be averaged with a connector
  one.

Usage the provider did not report stays `unknown`, never zero. An untested mode is `blocked`, never
`passed`. Run it as

    python evals/score.py                 # every channel that has records
    python evals/score.py --channel connector
"""
from __future__ import annotations

import argparse
import json
import pathlib
import sys

HERE = pathlib.Path(__file__).resolve().parent
RUNS_DIR = HERE / "runs"
TASKS_FILE = HERE / "tasks.json"
SUMMARY_FILE = HERE / "results" / "SUMMARY.md"

JUDGED = {"official", "third_party", "not_official"}


def load_records(channel: str) -> list[dict]:
    records = []
    for path in sorted(RUNS_DIR.glob("*.json")):
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except ValueError:
            continue
        if record.get("channel") == channel:
            records.append(record)
    return records


def _rate(numerator: int, denominator: int, reason: str):
    """A rate with no denominator is `None` plus the reason, never a number."""
    if denominator == 0:
        return {"value": None, "denominator": 0, "reason": reason}
    return {"value": round(numerator / denominator, 4), "numerator": numerator,
            "denominator": denominator}


def score(records: list[dict], tasks: dict, channel: str) -> dict:
    by_id = {task["id"]: task for task in tasks["tasks"]}
    statuses: dict[str, int] = {}
    steps = {"ok": 0, "unknown": 0, "contradicted": 0, "missed": 0, "deferred": 0, "human": 0}
    availability = {"positive": 0, "contradicted": 0}
    blocked = 0
    usage_unknown = 0

    for record in records:
        statuses[record.get("status", "?")] = statuses.get(record.get("status", "?"), 0) + 1
        if record.get("status") == "blocked":
            blocked += 1
        if (record.get("usage") or {}).get("provider_reported") == "unknown":
            usage_unknown += 1
        for step in record.get("steps", []):
            state = step.get("state", "unknown")
            steps[state] = steps.get(state, 0) + 1
            if step.get("type") == "resource_status_in" and step.get("status"):
                # A status the check produced is a positive claim; whether it agreed is separate.
                availability["positive"] += 1
                if state == "contradicted":
                    availability["contradicted"] += 1

    labels = [label for record in records for label in
              (by_id.get(record["task_id"], {}).get("labels") or [])]
    judged = [label for label in labels if label.get("verdict") in JUDGED]
    confirmed = [label for label in judged if label.get("verdict") == "official"]
    unjudged = [label for label in labels if label.get("verdict") not in JUDGED]
    human_checks = sum(1 for record in records for step in record.get("steps", [])
                       if step.get("state") == "human")
    retrieval_steps = steps["ok"] + steps["unknown"] + steps["contradicted"] + steps["missed"]

    return {
        "channel": channel,
        "task_set_version": tasks.get("task_set_version"),
        "tasks": {"total": len(records), "by_status": statuses,
                  "blocked_without_credentials": blocked},
        "steps": dict(steps),
        "official_candidate_accuracy": _rate(
            len(confirmed), len(judged),
            "no sample in this channel has been judged official yet"
            + (f"; {len(unjudged)} label(s) are recorded as unknown" if unjudged else "")),
        "resource_availability_false_positive_rate": _rate(
            availability["contradicted"], availability["positive"],
            "this channel produced no resource status to compare against"),
        "coverage_unknown_ratio": _rate(
            steps["unknown"], retrieval_steps,
            "this channel produced no retrieval step to judge"),
        # Recall misses are reported on their own: folding them into an accuracy rate would let a
        # completed-but-empty search count as agreement.
        "recall_misses": steps["missed"],
        "human_review_pending": human_checks,
        "usage": {"provider_reported": "unknown", "records_without_usage": usage_unknown,
                  "note": "a provider that reports no usage stays unknown; it is never counted as 0"},
        "pending": [
            "evidence_support_rate needs a completed live task with a report",
            "resource_availability_false_positive_rate needs a resource whose check contradicted a claim",
        ],
    }


def render_summary(metrics: dict) -> str:
    lines = [f"# Evaluation summary — channel `{metrics['channel']}`",
             "",
             f"- task set version: {metrics['task_set_version']}",
             f"- tasks run: {metrics['tasks']['total']} "
             f"({', '.join(f'{key}={value}' for key, value in sorted(metrics['tasks']['by_status'].items())) or 'none'})",
             f"- blocked for want of credentials: {metrics['tasks']['blocked_without_credentials']}",
             f"- steps: {', '.join(f'{key}={value}' for key, value in sorted(metrics['steps'].items()))}",
             f"- recall misses (expected identifier not in the results): {metrics['recall_misses']}",
             ""]
    for name, key in (("Official-candidate accuracy", "official_candidate_accuracy"),
                      ("Resource availability false-positive rate",
                       "resource_availability_false_positive_rate"),
                      ("Coverage unknown ratio", "coverage_unknown_ratio")):
        entry = metrics[key]
        if entry["value"] is None:
            lines.append(f"- **{name}: no value** — {entry['reason']}")
        else:
            lines.append(f"- **{name}: {entry['value']}** "
                         f"({entry['numerator']}/{entry['denominator']})")
    lines += ["",
              f"- human review still pending: {metrics['human_review_pending']} expectation(s)",
              f"- usage: {metrics['usage']['provider_reported']} reported by the provider "
              f"({metrics['usage']['records_without_usage']} record(s) without usage)",
              "",
              "## Not measured yet",
              ""]
    lines += [f"- {item}" for item in metrics["pending"]]
    lines += ["",
              "A `null` above is not a zero and not a one: it means the channel has no sample that "
              "would make the rate mean anything. Nothing here is a claim about research quality."]
    return "\n".join(lines) + "\n"


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--channel", default=None, choices=["connector", "live"])
    parser.add_argument("--json", dest="json_path", default=None)
    args = parser.parse_args(argv)

    tasks = json.loads(TASKS_FILE.read_text(encoding="utf-8"))
    channels = [args.channel] if args.channel else ["connector", "live"]
    summaries = []
    for channel in channels:
        records = load_records(channel)
        if not records:
            print(f"{channel}: no run records in {RUNS_DIR}")
            continue
        metrics = score(records, tasks, channel)
        summaries.append(render_summary(metrics))
        if args.json_path:
            pathlib.Path(args.json_path).write_text(json.dumps(metrics, ensure_ascii=False, indent=2),
                                                    encoding="utf-8")
    if not summaries:
        return 1
    combined = "\n".join(summaries)
    SUMMARY_FILE.parent.mkdir(exist_ok=True)
    SUMMARY_FILE.write_text(combined, encoding="utf-8")
    print(combined)
    print(f"written to {SUMMARY_FILE}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
