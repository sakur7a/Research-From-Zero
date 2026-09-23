"""Live Agent-to-resource-matrix flow with source/provider/model fixtures, not a JSON upload."""
import base64
import json
import time

import httpx
from fastapi.testclient import TestClient

from re0.agent.tools import TOOL_TYPES
from re0.main import create_app

CONFIG = {"base_url": "https://api.openai.com/v1", "model": "matrix-fixture",
          "api_key": "sk-matrix-fixture-not-a-real-key", "trust_endpoint": True}
GOAL = {"goal": "比较三篇 LayerKit 工作的代码资源并留下来源", "consent_to_send": True}
HEADERS = {"X-Re0-Client": "web", "Content-Type": "application/json"}

SHA = "a" * 40
PAPERS = [
    ("2501.11111v2", "LayerKit: Paper A", "Code: https://github.com/lab/shared-baseline"),
    ("2501.22222v1", "Paper B", "Uses baseline https://github.com/lab/shared-baseline; adapter candidate."),
    ("2501.33333v1", "LayerKit: Paper C", "A similar project name is not evidence of authorship."),
]


def atom_feed():
    entries = "".join(
        f"""<entry><id>http://arxiv.org/abs/{identifier}</id><title>{title}</title>
        <summary>{abstract}</summary><published>2025-02-01T00:00:00Z</published>
        <author><name>Fixture Author</name></author></entry>"""
        for identifier, title, abstract in PAPERS)
    return (f'<feed xmlns="http://www.w3.org/2005/Atom">{entries}</feed>').encode()


def tool_completion(call_id, name, args):
    return {"choices": [{"finish_reason": "tool_calls", "message": {
        "role": "assistant", "content": None, "tool_calls": [{
            "id": call_id, "type": "function",
            "function": {"name": name, "arguments": json.dumps(args, ensure_ascii=False)}}]}}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 4, "total_tokens": 14}}


def collect_evidence(messages):
    items = {}
    for message in messages:
        if message.get("role") != "tool":
            continue
        for item in json.loads(message["content"]).get("evidence", []):
            items[item["id"]] = item
    return list(items.values())


class MatrixFixture:
    def __init__(self):
        self.requests = []
        self.model_calls = 0
        self.sequence = 0
        self.tree_by_repo = {
            "lab/shared-baseline": ["train.py", "evaluate.py", "requirements.txt"],
            "lab/adapter-resource": ["train.py", "adapter_model.safetensors"],
            "someone/LayerKit": ["README.md"],
        }

    def __call__(self, request):
        self.requests.append(request)
        if request.url.host == "export.arxiv.org":
            return httpx.Response(200, content=atom_feed(), headers={"Content-Type": "application/atom+xml"})
        if request.url.host == "api.github.com":
            path = request.url.path.removeprefix("/repos/")
            if path == "lab/failing-resource":
                return httpx.Response(404, json={"message": "Not Found"})
            for repo, paths in self.tree_by_repo.items():
                if path == repo:
                    return httpx.Response(200, json={"default_branch": "main",
                                                     "license": {"spdx_id": "MIT"}})
                if path == f"{repo}/commits/main":
                    return httpx.Response(200, json={"sha": SHA})
                if path == f"{repo}/git/trees/{SHA}":
                    return httpx.Response(200, json={"truncated": False, "tree": [
                        {"type": "blob", "path": name} for name in paths]})
                if path == f"{repo}/releases":
                    return httpx.Response(200, json=[])
                if path == f"{repo}/readme":
                    raw = base64.b64encode(b"# Fixture repository\nNot an authorship claim.\n").decode()
                    return httpx.Response(200, json={"encoding": "base64", "path": "README.md",
                                                     "content": raw})
            raise AssertionError(f"unexpected repository request: {request.url}")

        assert request.url == "https://api.openai.com/v1/chat/completions"
        data = json.loads(request.content)
        assert data["model"] == CONFIG["model"]
        if isinstance(data.get("tool_choice"), dict):
            return httpx.Response(200, json=tool_completion(
                "matrix-connection-test", "connection_check", {}))
        self.model_calls += 1
        call = self.model_calls
        self.sequence += 1
        call_id = f"matrix-call-{self.sequence}"
        if call == 1:
            return httpx.Response(200, json=tool_completion(
                call_id, "update_plan", {"steps": ["查找三篇论文", "核验公开资源", "形成带来源的比较"]}))
        if call == 2:
            return httpx.Response(200, json=tool_completion(
                call_id, "search_papers", {"query": "LayerKit", "source": "arxiv", "limit": 3}))
        if call in {3, 4, 5, 6}:
            urls = ["https://github.com/lab/shared-baseline",
                    "https://github.com/lab/adapter-resource",
                    "https://github.com/someone/LayerKit",
                    "https://github.com/lab/failing-resource"]
            return httpx.Response(200, json=tool_completion(
                call_id, "inspect_resource", {"url": urls[call - 3]}))
        if call == 7:
            evidence = collect_evidence(data["messages"])
            papers = {item["paper"]["arxiv_id"]: item for item in evidence
                      if item.get("kind") == "paper" and item.get("paper")}
            resources = {item["source_url"]: item for item in evidence
                         if item.get("kind") == "resource_check"}
            a, b = papers["2501.11111v2"], papers["2501.22222v1"]
            baseline = resources["https://github.com/lab/shared-baseline"]
            adapter = resources["https://github.com/lab/adapter-resource"]
            links = [
                {"paper_evidence_id": a["id"], "resource_evidence_id": baseline["id"],
                 "relation_evidence_ids": [a["id"], baseline["id"]],
                 "rationale": "论文摘要列出同一个基线仓库；关联仍是候选，作者归属未确认。"},
                {"paper_evidence_id": b["id"], "resource_evidence_id": baseline["id"],
                 "relation_evidence_ids": [b["id"], baseline["id"]],
                 "rationale": "本轮证据把该仓库作为候选基线；需人工确认其适用范围。"},
                {"paper_evidence_id": b["id"], "resource_evidence_id": adapter["id"],
                 "relation_evidence_ids": [b["id"], adapter["id"]],
                 "rationale": "检查到了 adapter 权重候选；基础模型与论文版本尚未确认。"},
            ]
            result = {"title": "Fixture resource matrix", "summary": "仅使用离线来源 fixture。",
                      "findings": [{"claim": "候选文件存在于所检查的仓库清单中。",
                                    "evidence_ids": [a["id"], b["id"], baseline["id"], adapter["id"]],
                                    "assessment": "observed"}],
                      "resource_links": links,
                      "limitations": ["这是 fixture 流程，不是实时科研或作者归属确认。"],
                      "outcome": "findings"}
            return httpx.Response(200, json=tool_completion(call_id, "finish_report", result))
        raise AssertionError(f"unexpected model round {call}")


def wait_done(client, run_id):
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        response = client.get(f"/api/agent/runs/{run_id}")
        assert response.status_code == 200, response.text
        run = response.json()
        if run["status"] not in {"queued", "running"}:
            return run
        time.sleep(0.02)
    raise AssertionError("fixture Agent task did not finish")


def test_live_agent_run_builds_evidence_linked_matrix_and_batches_human_approval(tmp_path):
    schema, description = TOOL_TYPES["finish_report"]
    assert "resource_links" in schema.model_json_schema()["properties"]
    assert "candidate relation" in description.lower()
    fixture = MatrixFixture()
    with TestClient(create_app(str(tmp_path / "matrix.sqlite3"), httpx.MockTransport(fixture)),
                    headers=HEADERS) as client:
        assert client.put("/api/agent/config", json=CONFIG).status_code == 200
        started = client.post("/api/agent/runs", json=GOAL)
        assert started.status_code == 202, started.text
        run = wait_done(client, started.json()["id"])
        assert run["status"] == "completed", run
        assert len(run["report"]["resource_links"]) == 3

        response = client.get(f"/api/agent/runs/{run['id']}/matrix")
        assert response.status_code == 200, response.text
        matrix = response.json()
        assert matrix["schema_version"] == "2"
        assert matrix["coverage"]["agent_run"] == {
            "run_id": run["id"], "paper_evidence": 3, "proposed_links": 3, "unlinked_checks": 2}
        linked = [row for row in matrix["rows"] if row.get("resource_url")]
        absent = [row for row in matrix["rows"] if not row.get("resource_url")]
        assert len(linked) == 3 and len(absent) == 1
        assert sum(row["resource_url"] == "https://github.com/lab/shared-baseline" for row in linked) == 2
        assert all(row["attribution"] == "unconfirmed" and row["version_match"] == "unknown"
                   for row in linked)
        adapter = next(row for row in linked if row["resource_url"].endswith("/adapter-resource"))
        assert any("基础模型" in item for item in adapter["limitations"])
        assert len({row["paper_evidence_id"] for row in linked
                    if row["resource_url"].endswith("/shared-baseline")}) == 2
        assert any("someone/LayerKit" in item["resource_url"]
                   for item in matrix["unlinked_checks"])
        assert any(item["status"] == "access_failed" and item["provider_status"] == "HTTP 404"
                   for item in matrix["unlinked_checks"])
        assert not any(item["resource_url"].endswith("/LayerKit") for item in matrix["rows"])

        selections = [{"paper_evidence_id": item["paper_evidence_id"],
                       "resource_evidence_id": item["resource_evidence_id"]}
                      for item in matrix["approval_candidates"]]
        preview = client.post(f"/api/agent/runs/{run['id']}/matrix/preview",
                              json={"selections": selections})
        assert preview.status_code == 200, preview.text
        assert preview.json()["dry_run"] is True
        assert preview.json()["ready"] == 2
        assert client.get("/api/papers").json() == [], "preview must write no paper or resource"
        refused = client.post(f"/api/agent/runs/{run['id']}/matrix/preview", json={"selections": [
            {"paper_evidence_id": "ev_from_another_run", "resource_evidence_id":
             selections[0]["resource_evidence_id"]}]})
        assert refused.status_code == 404

        saved = client.post(f"/api/agent/runs/{run['id']}/matrix/confirm",
                            json={"selections": selections})
        assert saved.status_code == 200, saved.text
        assert len(saved.json()["created"]) == 2 and len(saved.json()["linked"]) == 3
        papers = client.get("/api/papers").json()
        assert len(papers) == 2
        repeated = client.post(f"/api/agent/runs/{run['id']}/matrix/confirm",
                               json={"selections": selections})
        assert repeated.status_code == 200, repeated.text
        assert repeated.json()["created"] == [] and repeated.json()["linked"] == []

        csv_response = client.get(f"/api/agent/runs/{run['id']}/matrix?format=csv")
        md_response = client.get(f"/api/agent/runs/{run['id']}/matrix?format=markdown")
        assert csv_response.status_code == md_response.status_code == 200
        assert "association_evidence_ids" in csv_response.text
        assert "待人工确认" in md_response.text and "unconfirmed" in md_response.text
