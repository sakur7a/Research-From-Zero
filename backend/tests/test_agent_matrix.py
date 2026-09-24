"""Live Agent-to-resource-matrix flow with source/provider/model fixtures, not a JSON upload."""
import base64
import csv
import io
import json
import threading
import time

import httpx
import pytest
from fastapi.testclient import TestClient

import re0.agent.runtime as runtime_module
from re0.agent.tools import TOOL_TYPES
from re0.main import create_app
from re0.resource_matrix import COMPONENT_LABELS

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


def inverted_index(text):
    index = {}
    for position, word in enumerate(text.split()):
        index.setdefault(word, []).append(position)
    return index


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
    def __init__(self, *, five_queries_three_pages=False):
        self.requests = []
        self.model_calls = 0
        self.sequence = 0
        self.five_queries_three_pages = five_queries_three_pages
        self.queries = [f"layer decomposition wording {index}" for index in range(5)]
        self.tree_by_repo = {
            "lab/shared-baseline": ["train.py", "evaluate.py", "requirements.txt"],
            "lab/adapter-resource": ["train.py", "adapter_model.safetensors"],
            "someone/LayerKit": ["README.md"],
        }

    def __call__(self, request):
        self.requests.append(request)
        if request.url.host == "export.arxiv.org":
            return httpx.Response(200, content=atom_feed(), headers={"Content-Type": "application/atom+xml"})
        if request.url.host == "api.openalex.org":
            assert self.five_queries_three_pages, request.url
            query_index = self.queries.index(request.url.params["search"])
            cursor = request.url.params["cursor"]
            page_index = 0 if cursor == "*" else int(cursor)
            if query_index == 0 and page_index == 0:
                records = [
                    {"id": f"https://openalex.org/W2501{index}",
                     "doi": f"https://doi.org/10.48550/arXiv.{identifier}",
                     "title": title, "publication_year": 2025, "type": "preprint",
                     "authorships": [{"author": {"display_name": "Fixture Author"},
                                      "institutions": []}],
                     "primary_location": {"source": {"display_name": "arXiv",
                                                        "type": "repository"}},
                     "abstract_inverted_index": inverted_index(abstract), "cited_by_count": 1}
                    for index, (identifier, title, abstract) in enumerate(PAPERS)
                ]
            else:
                records = []
                for item_index in range(3):
                    serial = 3 + (query_index * 3 + page_index) * 3 + item_index
                    identifier = f"2601.{serial:05d}"
                    abstract = ("Long paginated abstract. " + "evidence detail " * 450
                                + f"query {query_index} page {page_index} result {item_index}.")
                    records.append({
                        "id": f"https://openalex.org/W{serial:08d}",
                        "doi": f"https://doi.org/10.48550/arXiv.{identifier}",
                        "title": f"Paged fixture work {serial}", "publication_year": 2025,
                        "type": "preprint",
                        "authorships": [{"author": {"display_name": "Fixture Author"},
                                         "institutions": []}],
                        "primary_location": {"source": {"display_name": "arXiv",
                                                           "type": "repository"}},
                        "abstract_inverted_index": inverted_index(abstract), "cited_by_count": serial,
                    })
            return httpx.Response(200, json={
                "results": records,
                "meta": {"next_cursor": str(page_index + 1) if page_index < 2 else None},
            })
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
            search_args = ({"query": self.queries[0], "queries": self.queries,
                            "source": "openalex", "limit": 3, "max_pages": 3,
                            "max_requests": 40} if self.five_queries_three_pages else
                           {"query": "LayerKit", "source": "arxiv", "limit": 3})
            return httpx.Response(200, json=tool_completion(
                call_id, "search_papers", search_args))
        if call in {3, 4, 5, 6}:
            urls = ["https://github.com/lab/shared-baseline",
                    "https://github.com/lab/adapter-resource",
                    "https://github.com/someone/LayerKit",
                    "https://github.com/lab/failing-resource"]
            return httpx.Response(200, json=tool_completion(
                call_id, "inspect_resource", {"url": urls[call - 3]}))
        if call == 7:
            evidence = collect_evidence(data["messages"])
            if self.five_queries_three_pages:
                assert sum(item.get("kind") == "paper" for item in evidence) == 45
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


class SharedBudgetFixture:
    """A second tool cannot spend the final request reserved for the structured report."""
    def __init__(self):
        self.requests = []
        self.model_calls = 0
        self.sequence = 0

    def __call__(self, request):
        self.requests.append(request)
        if request.url.host == "export.arxiv.org":
            return httpx.Response(200, content=atom_feed(), headers={"Content-Type": "application/atom+xml"})
        if request.url.host == "api.github.com":
            assert request.url.path == "/repos/lab/should-not-request", request.url
            return httpx.Response(200, json={"default_branch": "main", "license": {"spdx_id": "MIT"}})
        assert request.url == "https://api.openai.com/v1/chat/completions"
        data = json.loads(request.content)
        self.model_calls += 1
        self.sequence += 1
        call_id = f"shared-budget-{self.sequence}"
        if self.model_calls == 1:
            name, args = "update_plan", {"steps": ["查找一篇论文", "核验资源", "提交结果"]}
        elif self.model_calls == 2:
            name, args = "search_papers", {"query": "LayerKit", "source": "arxiv", "limit": 3}
        elif self.model_calls == 3:
            name, args = "inspect_resource", {"url": "https://github.com/lab/should-not-request"}
        elif self.model_calls == 4:
            tools = [item["function"]["name"] for item in data.get("tools", [])]
            assert tools == ["finish_report"], tools
            papers = [item for item in collect_evidence(data["messages"])
                      if item.get("kind") == "paper" and item.get("paper")]
            assert papers
            report = {"title": "Budget stopped before resource inspection",
                      "summary": "The paper is retained; the resource check was blocked before HTTP.",
                      "findings": [{"claim": "A paper metadata record was retrieved.",
                                    "evidence_ids": [papers[0]["id"]], "assessment": "observed"}],
                      "limitations": ["The task-wide upstream request budget stopped resource inspection."],
                      "outcome": "findings"}
            return httpx.Response(200, json=tool_completion(call_id, "finish_report", report))
        else:
            raise AssertionError(f"unexpected model request {self.model_calls}")
        return httpx.Response(200, json=tool_completion(call_id, name, args))


class DuplicateToolFixture:
    """An exact repeated search reuses evidence without spending another provider request."""
    def __init__(self):
        self.requests = []
        self.model_calls = 0

    def __call__(self, request):
        self.requests.append(request)
        if request.url.host == "export.arxiv.org":
            return httpx.Response(200, content=atom_feed(), headers={"Content-Type": "application/atom+xml"})
        assert request.url == "https://api.openai.com/v1/chat/completions"
        data = json.loads(request.content)
        self.model_calls += 1
        call_id = f"duplicate-{self.model_calls}"
        if self.model_calls == 1:
            name, args = "update_plan", {"steps": ["搜索论文", "复用已有结果", "提交报告"]}
        elif self.model_calls in {2, 3}:
            name, args = "search_papers", {"query": "LayerKit", "source": "arxiv", "limit": 3}
        elif self.model_calls == 4:
            name, args = "search_papers", {"query": "LayerKit", "source": "arxiv", "limit": 3,
                                          "refresh": True}
        elif self.model_calls == 5:
            evidence = collect_evidence(data["messages"])
            papers = {item["paper"]["arxiv_id"]: item for item in evidence
                      if item.get("kind") == "paper" and item.get("paper")}
            assert len(papers) == 3
            report = {"title": "Repeated search observed", "summary": "Repeated input is visible; explicit refresh requests a new source snapshot.",
                      "findings": [{"claim": "Three paper records are retained.",
                                    "evidence_ids": [item["id"] for item in papers.values()], "assessment": "observed"}],
                      "limitations": ["The fixture does not validate live retrieval quality."],
                      "outcome": "findings"}
            return httpx.Response(200, json=tool_completion(call_id, "finish_report", report))
        else:
            raise AssertionError(f"unexpected model round {self.model_calls}")
        return httpx.Response(200, json=tool_completion(call_id, name, args))


class CancelledSearchFixture:
    """Cancellation after one source response blocks the next source and keeps prior rows."""
    def __init__(self):
        self.requests = []
        self.model_calls = 0
        self.source_started = threading.Event()
        self.release_source = threading.Event()

    def __call__(self, request):
        self.requests.append(request)
        if request.url.host == "export.arxiv.org":
            self.source_started.set()
            assert self.release_source.wait(5)
            return httpx.Response(200, content=atom_feed(), headers={"Content-Type": "application/atom+xml"})
        assert request.url == "https://api.openai.com/v1/chat/completions"
        self.model_calls += 1
        call_id = f"cancel-search-{self.model_calls}"
        if self.model_calls == 1:
            return httpx.Response(200, json=tool_completion(
                call_id, "update_plan", {"steps": ["检索候选来源", "保存已取得材料", "停止后续请求"]}))
        if self.model_calls == 2:
            return httpx.Response(200, json=tool_completion(
                call_id, "search_papers", {"query": "LayerKit", "sources": ["arxiv", "crossref"], "limit": 3}))
        raise AssertionError(f"cancelled task unexpectedly made model request {self.model_calls}")


class DeadlineClock:
    def __init__(self):
        self.now = 0.0

    def monotonic(self):
        return self.now

    def expire(self):
        self.now = 30.0


class DeadlineSearchFixture:
    """An elapsed attempt deadline blocks the next source after retaining arXiv evidence."""
    def __init__(self, clock):
        self.clock = clock
        self.requests = []
        self.model_calls = 0

    def __call__(self, request):
        self.requests.append(request)
        if request.url.host == "export.arxiv.org":
            self.clock.expire()
            return httpx.Response(200, content=atom_feed(), headers={"Content-Type": "application/atom+xml"})
        if request.url.host == "api.crossref.org":
            raise AssertionError("deadline must refuse the next source before HTTP dispatch")
        assert request.url == "https://api.openai.com/v1/chat/completions"
        self.model_calls += 1
        call_id = f"deadline-search-{self.model_calls}"
        if self.model_calls == 1:
            return httpx.Response(200, json=tool_completion(
                call_id, "update_plan", {"steps": ["检索候选来源", "保留已取得材料"]}))
        if self.model_calls == 2:
            return httpx.Response(200, json=tool_completion(
                call_id, "search_papers", {"query": "LayerKit", "sources": ["arxiv", "crossref"], "limit": 3}))
        raise AssertionError(f"deadline task unexpectedly made model request {self.model_calls}")


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
        model_requests = [item for item in fixture.requests if item.url.path.endswith("/chat/completions")]
        request_sizes = [len(item.content) for item in model_requests]
        request_chars = [len(item.content.decode("utf-8")) for item in model_requests]
        assert run["upstream_requests"] == len(fixture.requests)
        assert run["model_request_bytes"] == sum(request_sizes)
        assert run["model_request_chars"] == sum(request_chars)
        assert run["largest_model_request_bytes"] == max(request_sizes)
        assert run["largest_model_request_chars"] == max(request_chars)
        progress = client.get(f"/api/agent/runs/{run['id']}/progress?after=0&evidence_after=0")
        assert progress.status_code == 200, progress.text
        progress_data = progress.json()
        assert progress_data["run"]["status"] == "completed"
        assert len(progress_data["evidence"]) == progress_data["run"]["evidence_count"] == len(run["evidence"])
        quiet = client.get(f"/api/agent/runs/{run['id']}/progress?after={progress_data['next_event_id']}"
                           f"&evidence_after={progress_data['evidence_cursor']}").json()
        assert quiet["events"] == [] and quiet["evidence"] == []

        response = client.get(f"/api/agent/runs/{run['id']}/matrix")
        assert response.status_code == 200, response.text
        matrix = response.json()
        assert matrix["schema_version"] == "3"
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
        exported_rows = list(csv.DictReader(io.StringIO(csv_response.text)))
        exported_by_link = {(row["paper_evidence_id"], row["resource_evidence_id"]): row
                            for row in exported_rows}
        assert len(exported_rows) == len(matrix["rows"])
        for row in matrix["rows"]:
            exported = exported_by_link[(row["paper_evidence_id"], row["resource_evidence_id"])]
            assert exported["work_identifier"] == row["work_identifier"]
            assert exported["work_version"] == row["work_version"]
            assert exported["resource_url"] == row["resource_url"]
            assert exported["status"] == row["status"]
            assert exported["checked_at"] == row["checked_at"]
            assert exported["scope"] == row["scope"]
            assert exported["association_evidence_ids"] == "; ".join(row["association_evidence_ids"])
            assert exported["association_sources"] == "; ".join(row["association_sources"])
            if row["resource_url"]:
                markdown_row = next(line for line in md_response.text.splitlines()
                                    if row["paper_title"] in line and row["resource_url"] in line)
                assert row["scope"] in markdown_row
                assert row["checked_at"] in markdown_row
                assert all(evidence_id in markdown_row
                           for evidence_id in row["association_evidence_ids"])

        for row in linked:
            paper = next(item for item in papers
                         if item["arxiv_id"] == row["work_identifier"]
                         and item["title"] == row["paper_title"])
            resource = next(item for item in paper["resources"] if item["url"] == row["resource_url"])
            audit = resource["latest"]
            assert audit["resource_url"] == row["resource_url"]
            assert audit["work_identifier"] == row["work_identifier"]
            assert audit["work_version"] == row["work_version"] == resource["applicable_version"]
            assert audit["status"] == row["status"]
            assert audit["checked_at"] == row["checked_at"]
            assert audit["scope"] == row["scope"]
            assert audit["attribution"] == row["attribution"] == "unconfirmed"
            assert audit["version_match"] == row["version_match"] == "unknown"
            for name, state in row["coverage"].items():
                raw_state = audit["coverage"].get(name, {}).get("state", "unknown")
                assert state == COMPONENT_LABELS[raw_state]
            audit_sources = {source["source_url"] for source in audit["evidence"]}
            audit_sources.update(source for finding in audit["coverage"].values()
                                 for source in finding["sources"])
            assert set(row["sources"]).issubset(audit_sources)
            approvals = [item for item in audit["agent_association_approvals"]
                         if item["resource_evidence_id"] == row["resource_evidence_id"]]
            assert len(approvals) == 1
            approval = approvals[0]
            assert approval["run_id"] == run["id"]
            assert approval["decision"] == "confirmed_by_user"
            assert approval["resource_checked_at"] == row["checked_at"]
            assert approval["resource_scope"] == row["scope"]
            assert approval["proposal_evidence_ids"] == row["association_evidence_ids"]
            assert approval["proposal_sources"] == row["association_evidence"]
            assert approval["proposal_note"] == row["association_note"]

        library_export = client.get("/api/export").json()
        for paper in papers:
            exported_paper = next(item for item in library_export["papers"]
                                  if item["id"] == paper["id"])
            for resource in paper["resources"]:
                exported_resource = next(item for item in exported_paper["resources"]
                                         if item["id"] == resource["id"])
                assert exported_resource["observations"][-1]["agent_association_approvals"] \
                    == resource["latest"]["agent_association_approvals"]

        # Progress is bounded per response; clients can drain the remainder without losing a cursor.
        with client.app.state.store.db.connect() as con:
            event_cursor = con.execute(
                "SELECT COALESCE(MAX(id),0) FROM agent_events WHERE run_id=?", (run["id"],)
            ).fetchone()[0]
            evidence_cursor = con.execute(
                "SELECT COUNT(*) FROM agent_evidence WHERE run_id=?", (run["id"],)
            ).fetchone()[0]
            for index in range(201):
                con.execute("INSERT INTO agent_events(run_id,at,kind,data) VALUES (?,?,?,?)",
                            (run["id"], "2026-09-24T00:00:00+00:00", "fixture_progress",
                             json.dumps({"message": str(index)})))
            for index in range(60):
                con.execute("INSERT INTO agent_evidence(id,run_id,tool_call_id,data) VALUES (?,?,?,?)",
                            (f"ev_progress_{index}", run["id"], f"progress_{index}",
                             json.dumps({"kind": "fixture", "source_url": "https://example.org",
                                         "content": str(index)})))
        first_page = client.get(
            f"/api/agent/runs/{run['id']}/progress?after={event_cursor}"
            f"&evidence_after={evidence_cursor}").json()
        assert len(first_page["events"]) == 200 and first_page["has_more_events"] is True
        assert len(first_page["evidence"]) == 50 and first_page["has_more_evidence"] is True
        second_page = client.get(
            f"/api/agent/runs/{run['id']}/progress?after={first_page['next_event_id']}"
            f"&evidence_after={first_page['evidence_cursor']}").json()
        assert len(second_page["events"]) == 1 and second_page["has_more_events"] is False
        assert len(second_page["evidence"]) == 10 and second_page["has_more_evidence"] is False


@pytest.mark.parametrize("five_queries_three_pages", [False, True],
                         ids=["one-query", "five-queries-three-pages"])
def test_long_papers_and_resource_audits_fit_the_measured_model_request_budget(
        tmp_path, monkeypatch, five_queries_three_pages):
    monkeypatch.setattr(__import__(__name__), "PAPERS", [
        (identifier, title, "Long fixture abstract. " + "evidence detail " * 900)
        for identifier, title, _ in PAPERS])
    fixture = MatrixFixture(five_queries_three_pages=five_queries_three_pages)
    long_paths = ([f"experiments/long-path/for-evaluation-and-training-artifacts/{index:03d}_train.py"
                   for index in range(60)]
                  + [f"data/train_split_{index:03d}.parquet" for index in range(20)])
    fixture.tree_by_repo = {name: long_paths for name in fixture.tree_by_repo}
    with TestClient(create_app(str(tmp_path / "long-context.sqlite3"), httpx.MockTransport(fixture)),
                    headers=HEADERS) as client:
        assert client.put("/api/agent/config", json=CONFIG).status_code == 200
        started = client.post("/api/agent/runs", json=GOAL)
        assert started.status_code == 202, started.text
        run = wait_done(client, started.json()["id"])
        assert run["status"] == "completed", {"error": run.get("error"),
                                               "model_calls": run.get("model_calls"),
                                               "tool_calls": run.get("tool_calls"),
                                               "upstream_requests": run.get("upstream_requests")}
        model_requests = [item for item in fixture.requests if item.url.path.endswith("/chat/completions")]
        request_chars = [len(item.content.decode("utf-8")) for item in model_requests]
        request_bytes = [len(item.content) for item in model_requests]
        assert run["model_calls"] == 7 and run["tool_calls"] == 7
        assert run["upstream_requests"] == len(fixture.requests)
        assert run["model_request_chars"] == sum(request_chars)
        assert run["model_request_bytes"] == sum(request_bytes)
        assert max(request_chars) == run["largest_model_request_chars"] < 150_000
        assert max(request_bytes) == run["largest_model_request_bytes"] < 150_000
        assert len(run["report"]["resource_links"]) == 3
        papers = [item for item in run["evidence"] if item.get("kind") == "paper"]
        if five_queries_three_pages:
            openalex_requests = [item for item in fixture.requests if item.url.host == "api.openalex.org"]
            assert len(openalex_requests) == 15
            assert {item.url.params["search"] for item in openalex_requests} == set(fixture.queries)
            assert all([item.url.params["cursor"] for item in openalex_requests
                        if item.url.params["search"] == query] == ["*", "1", "2"]
                       for query in fixture.queries)
            assert len(papers) == 45, sorted(item["paper"].get("arxiv_id", "") for item in papers)
            assert run["upstream_requests"] == len(fixture.requests)
            source_host, locator_source = "https://doi.org/10.48550/arXiv.", "openalex"
        else:
            assert run["upstream_requests"] == len(fixture.requests) == 24
            assert len(papers) == 3 and len(run["evidence"]) == 29
            source_host, locator_source = "https://arxiv.org/abs/", "arxiv"
        all_ids = {item["id"] for item in run["evidence"]}
        cited_ids = {evidence_id for finding in run["report"]["findings"]
                     for evidence_id in finding["evidence_ids"]}
        assert cited_ids and cited_ids <= all_ids
        cited_papers = [item for item in papers if item["id"] in cited_ids]
        assert cited_papers and all(item["source_url"].startswith(source_host)
                                    and item["locator"].startswith(f"metadata from {locator_source}")
                                    for item in cited_papers)
        assert max(len(item["paper"]["abstract"]) for item in papers) > 7_500


def test_task_wide_upstream_budget_spans_model_and_tools_and_reserves_a_report_call(tmp_path):
    fixture = SharedBudgetFixture()
    with TestClient(create_app(str(tmp_path / "shared-budget.sqlite3"), httpx.MockTransport(fixture)),
                    headers=HEADERS) as client:
        assert client.put("/api/agent/config", json=CONFIG).status_code == 200
        started = client.post("/api/agent/runs", json={**GOAL, "max_upstream_requests": 6})
        assert started.status_code == 202, started.text
        run = wait_done(client, started.json()["id"])
        assert run["status"] == "completed", {"error": run.get("error"),
                                               "counts": (run.get("model_calls"), run.get("upstream_requests")),
                                               "model_calls": fixture.model_calls, "run": run}
        assert run["upstream_requests"] == len(fixture.requests) == 6
        assert run["model_calls"] == 4
        assert len(run["evidence"]) == 3 and all(item["kind"] == "paper" for item in run["evidence"])
        assert run["report"]["resource_links"] == []
        progress = client.get(f"/api/agent/runs/{run['id']}/progress?after=0").json()
        blocked = next(item for item in progress["events"]
                       if item["kind"] == "tool_finished" and item["data"].get("tool") == "inspect_resource")
        assert blocked["data"]["ok"] is False
        assert "保留最后一次模型请求" in blocked["data"]["error"]


def test_repeated_exact_tool_input_reuses_task_evidence_without_an_upstream_request(tmp_path):
    fixture = DuplicateToolFixture()
    with TestClient(create_app(str(tmp_path / "duplicate-tool.sqlite3"), httpx.MockTransport(fixture)),
                    headers=HEADERS) as client:
        assert client.put("/api/agent/config", json=CONFIG).status_code == 200
        started = client.post("/api/agent/runs", json={**GOAL, "max_upstream_requests": 20})
        assert started.status_code == 202, started.text
        run = wait_done(client, started.json()["id"])
        assert run["status"] == "completed", run
        assert run["model_calls"] == 5 and run["tool_calls"] == 5
        assert run["upstream_requests"] == len(fixture.requests) == 7
        assert sum(item.url.host == "export.arxiv.org" for item in fixture.requests) == 2
        assert len(run["evidence"]) == 9
        progress = client.get(f"/api/agent/runs/{run['id']}/progress?after=0").json()
        warning = next(item for item in progress["events"] if item["kind"] == "tool_repeat_warning")
        assert warning["data"]["previous_call_id"] == "call_re0_2_0"
        assert "仍会按本次请求执行" in warning["data"]["message"]


def test_cancel_between_search_sources_keeps_completed_source_and_starts_no_next_request(tmp_path):
    fixture = CancelledSearchFixture()
    with TestClient(create_app(str(tmp_path / "cancel-search.sqlite3"), httpx.MockTransport(fixture)),
                    headers=HEADERS) as client:
        assert client.put("/api/agent/config", json=CONFIG).status_code == 200
        started = client.post("/api/agent/runs", json=GOAL)
        assert started.status_code == 202, started.text
        run_id = started.json()["id"]
        assert fixture.source_started.wait(5)
        assert client.post(f"/api/agent/runs/{run_id}/cancel", json={}).status_code == 200
        fixture.release_source.set()
        run = wait_done(client, run_id)
        assert run["status"] == "cancelled", run
        assert run["model_calls"] == 2 and run["tool_calls"] == 2
        assert run["upstream_requests"] == len(fixture.requests) == 3
        assert len(run["evidence"]) == 3 and all(item["kind"] == "paper" for item in run["evidence"])
        assert fixture.model_calls == 2
        cached = client.app.state.agent.tasks.cached_tool(run_id, "call_re0_2_0")
        assert cached["coverage"]["state"] == "cancelled"
        assert any(row["source"] == "crossref" and row["stop_reason"] == "cancelled"
                   for row in cached["coverage"]["pagination"])


def test_deadline_between_search_sources_keeps_completed_source_and_starts_no_next_request(tmp_path, monkeypatch):
    clock = DeadlineClock()
    monkeypatch.setattr(runtime_module, "time", clock)
    fixture = DeadlineSearchFixture(clock)
    with TestClient(create_app(str(tmp_path / "deadline-search.sqlite3"), httpx.MockTransport(fixture)),
                    headers=HEADERS) as client:
        assert client.put("/api/agent/config", json=CONFIG).status_code == 200
        started = client.post("/api/agent/runs", json={**GOAL, "attempt_seconds": 30})
        assert started.status_code == 202, started.text
        run = wait_done(client, started.json()["id"])
        assert run["status"] == "budget_exhausted", run
        assert run["model_calls"] == fixture.model_calls == 2
        assert run["upstream_requests"] == len(fixture.requests) == 3
        assert not any(item.url.host == "api.crossref.org" for item in fixture.requests)
        assert len(run["evidence"]) == 3 and all(item["kind"] == "paper" for item in run["evidence"])
        cached = client.app.state.agent.tasks.cached_tool(run["id"], "call_re0_2_0")
        assert cached["coverage"]["state"] == "time_budget"
        assert any(row["source"] == "crossref" and row["stop_reason"] == "time_budget"
                   for row in cached["coverage"]["pagination"])
