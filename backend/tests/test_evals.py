"""Tests for the evaluation harness itself.

The harness exists to stop the project from reporting a miss as a pass, so it has to be held to the
same standard. These are deterministic and touch no network: the tools are given a mock transport.
"""
import importlib.util
import json
import pathlib

import httpx
import pytest

from re0.agent.tools import ResearchTools

EVALS = pathlib.Path(__file__).resolve().parents[2] / "evals"


def load(name):
    spec = importlib.util.spec_from_file_location(f"evals_{name}", EVALS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def empty_arxiv():
    return ResearchTools(None, httpx.MockTransport(
        lambda request: httpx.Response(200, content=b'<feed xmlns="http://www.w3.org/2005/Atom"></feed>')))


def test_the_task_set_is_valid_and_every_expectation_says_who_checks_it():
    tasks = json.loads((EVALS / "tasks.json").read_text(encoding="utf-8"))
    assert tasks["task_set_version"] and tasks["rules"]
    ids = [task["id"] for task in tasks["tasks"]]
    assert len(ids) == len(set(ids)), "task ids must be unique"
    for task in tasks["tasks"]:
        assert task["channel"] in {"connector", "live"}
        assert task["kind"] in {"given_paper_and_repo", "find_resources", "constrained_selection"}
        assert task["input"], task["id"]
        for expectation in task["expectations"]:
            assert expectation["check"] in {"retrieval", "human"}, expectation
            assert expectation["type"], expectation


def test_no_label_claims_authorship_without_a_human_provenance_record():
    """A name match is not authorship, so an `official` verdict has to name who checked, when, and
    over what scope. Machine-filled labels must stay `unknown`."""
    tasks = json.loads((EVALS / "tasks.json").read_text(encoding="utf-8"))
    for task in tasks["tasks"]:
        for label in task.get("labels") or []:
            assert label["verdict"] in {"official", "third_party", "not_official", "unknown"}
            if label["verdict"] == "official":
                assert label.get("by") and label.get("by") != "machine"
                assert label.get("at") and label.get("scope")
            else:
                assert label.get("reason"), label


def test_a_recall_miss_is_recorded_as_a_miss_and_not_as_completion():
    """The regression the harness caught in its own first run: a completed search whose results do
    not contain the expected identifier was reported as `completed`."""
    runner = load("runner")
    task = {"id": "probe", "channel": "connector", "kind": "constrained_selection",
            "input": {"query": "layer decomposition", "source": "arxiv", "limit": 1},
            "expectations": [{"check": "retrieval", "type": "found_identifiers",
                              "value": ["arXiv:2605.30257"]}]}
    record = runner.run_task(empty_arxiv(), task)
    step = record["steps"][0]
    assert step["state"] == "missed"
    assert step["missing"] == ["arXiv:2605.30257"]
    # A miss is a coverage gap, and the step has to say so rather than read as absence.
    assert "not evidence that the work does not exist" in step["note"]
    assert record["status"] == "partial"


def test_a_step_that_did_not_complete_is_unknown_not_a_negative_result():
    runner = load("runner")

    def refuse(request):
        return httpx.Response(500, content=b"upstream detail")

    tools = ResearchTools(None, httpx.MockTransport(refuse))
    steps = []
    trace = []
    step = runner.check_identifier(tools, "arXiv:2501.1", trace)
    assert step["state"] == "unknown" and "did not complete" in step["detail"]
    assert trace and trace[0]["ok"] is False


def test_a_rate_with_no_denominator_is_no_value_never_a_perfect_score():
    scorer = load("score")
    tasks = json.loads((EVALS / "tasks.json").read_text(encoding="utf-8"))
    records = [{"task_id": "recall-reveallayer", "channel": "connector", "status": "completed",
                "steps": [{"type": "found_identifiers", "state": "ok"}],
                "usage": {"provider_reported": "unknown"}}]
    metrics = scorer.score(records, tasks, "connector")
    accuracy = metrics["official_candidate_accuracy"]
    assert accuracy["value"] is None and accuracy["denominator"] == 0
    assert "judged official" in accuracy["reason"]
    # No label in the public set is judged official yet, so that stays true of the real set too.
    assert accuracy["value"] != 1.0


def test_a_blocked_live_record_is_never_counted_as_a_pass_and_channels_do_not_mix():
    runner = load("runner")
    scorer = load("score")
    tasks = json.loads((EVALS / "tasks.json").read_text(encoding="utf-8"))
    live_task = next(task for task in tasks["tasks"] if task["channel"] == "live")

    record = runner.run_task(empty_arxiv(), live_task)
    assert record["status"] == "blocked"
    assert "no model configuration" in record["blocked_reason"]
    assert record["steps"] == []

    connector = [{"task_id": "recall-reveallayer", "channel": "connector", "status": "completed",
                  "steps": [{"type": "found_identifiers", "state": "ok"}],
                  "usage": {"provider_reported": "unknown"}}]
    connector_metrics = scorer.score(connector, tasks, "connector")
    assert connector_metrics["tasks"]["blocked_without_credentials"] == 0
    live_metrics = scorer.score([record], tasks, "live")
    assert live_metrics["tasks"]["blocked_without_credentials"] == 1
    # The blocked live record must not appear in the connector summary.
    assert connector_metrics["tasks"]["total"] == 1


def test_a_run_record_never_carries_a_credential_value():
    """Records are committed as the public history, so a value must never reach one."""
    runner = load("runner")
    record = runner.run_task(empty_arxiv(), {"id": "probe", "channel": "connector",
                                            "kind": "constrained_selection",
                                            "input": {"query": "x", "source": "arxiv", "limit": 1},
                                            "expectations": []})
    serialized = json.dumps(record, ensure_ascii=False)
    assert "credential_names_set" in record
    for pattern in ("sk-", "ghp_", "gho_", "Bearer ", "Authorization"):
        assert pattern not in serialized, pattern


def test_the_summary_states_a_null_instead_of_printing_a_number():
    scorer = load("score")
    rendered = scorer.render_summary(scorer.score([], {"task_set_version": "1", "tasks": []}, "live"))
    assert "no value" in rendered
    assert "not a zero and not a one" in rendered


def test_scoring_one_channel_preserves_the_other_channel_summary():
    scorer = load("score")
    existing = ("# Evaluation summary — channel `connector`\n\nold connector\n\n"
                "# Evaluation summary — channel `live`\n\nexisting live\n")
    replacement = "# Evaluation summary — channel `connector`\n\nnew connector\n"

    merged = scorer.merge_summaries(existing, [replacement])

    assert "new connector" in merged
    assert "old connector" not in merged
    assert "existing live" in merged
    assert merged.count("# Evaluation summary — channel") == 2
