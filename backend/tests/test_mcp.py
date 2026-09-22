"""Protocol tests for the MCP stdio surface.

These drive the server in-process, so verifying it needs no MCP dependency. The
handshake was additionally checked against the official `mcp` client during
development (see docs/TESTING.md); that check is not part of this suite because it
would pull a large dependency tree into the test extra.
"""
import io
import json
import pathlib

import httpx

from re0 import mcp_server
from re0.agent.tools import ResearchTools

ATOM = b'''<feed xmlns="http://www.w3.org/2005/Atom"><entry><id>http://arxiv.org/abs/2501.12345v2</id><title>Fixture Layout Paper</title><summary>TEST FIXTURE: a layout study. This is not a real paper claim.</summary><published>2025-01-21T00:00:00Z</published><author><name>Test Author</name></author></entry></feed>'''

EXPECTED_TOOLS = ["search_papers", "resolve_paper", "search_repositories", "search_hub",
                  "inspect_resource", "read_repository_file", "search_release_discussions"]


def exchange(messages, tools=None):
    out = io.StringIO()
    mcp_server.serve(io.StringIO("".join(json.dumps(message) + "\n" for message in messages)), out, tools)
    return [json.loads(line) for line in out.getvalue().splitlines() if line]


def paper_search_call():
    return {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": "search_papers", "arguments": {"query": "layout", "limit": 1, "source": "arxiv"}}}


def test_handshake_echoes_the_client_version_and_exposes_only_read_only_tools(monkeypatch):
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    initialize, listing = exchange([
        {"jsonrpc": "2.0", "id": 1, "method": "initialize",
         "params": {"protocolVersion": "2025-11-25", "capabilities": {}, "clientInfo": {"name": "t", "version": "0"}}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
    ])
    assert initialize["result"]["serverInfo"] == {"name": "re0-research", "version": mcp_server.SERVER_VERSION}
    assert initialize["result"]["capabilities"] == {"tools": {}}
    # Echoing the requested version keeps a newer client's handshake acceptable.
    assert initialize["result"]["protocolVersion"] == "2025-11-25"
    names = [tool["name"] for tool in listing["result"]["tools"]]
    assert names == EXPECTED_TOOLS
    for tool in listing["result"]["tools"]:
        assert tool["description"] and tool["inputSchema"]["type"] == "object"
    # Task-protocol, task-scoped and consent-gated tools must never appear here.
    assert "search_web" not in names


def test_notifications_and_malformed_lines_produce_no_response():
    assert exchange([{"jsonrpc": "2.0", "method": "notifications/initialized"},
                     {"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {}}]) == []
    out = io.StringIO()
    mcp_server.serve(io.StringIO("\n   \nnot json\n[1,2]\n"), out)
    assert out.getvalue() == ""


def test_unknown_methods_and_unexposed_tools_are_reported_not_guessed():
    unknown, hidden, task_tool, bad = exchange([
        {"jsonrpc": "2.0", "id": 1, "method": "resources/list"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "read_evidence", "arguments": {}}},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "finish_report", "arguments": {}}},
        {"jsonrpc": "2.0", "id": 4, "method": "tools/call",
         "params": {"name": "search_papers", "arguments": {"query": "x", "limit": 99}}},
    ])
    assert unknown["error"]["code"] == -32601
    for answer in (hidden, task_tool, bad):
        assert answer["result"]["isError"] is True
    assert "read_evidence" in hidden["result"]["content"][0]["text"]
    assert "finish_report" in task_tool["result"]["content"][0]["text"]
    # Validation failures report the offending field instead of a pydantic traceback.
    assert "limit" in bad["result"]["content"][0]["text"]
    assert "Traceback" not in bad["result"]["content"][0]["text"]


def test_a_tool_call_returns_bounded_text_with_a_locator():
    def transport(request):
        assert request.url.host == "export.arxiv.org"
        return httpx.Response(200, content=ATOM)
    (answer,) = exchange([paper_search_call()], ResearchTools(None, httpx.MockTransport(transport)))
    assert answer["result"]["isError"] is False
    text = answer["result"]["content"][0]["text"]
    assert "Fixture Layout Paper" in text and "TEST FIXTURE" in text
    assert "locator:" in text and "source:" in text and "documents: 1" in text
    assert len(text) < 4000


def test_the_surface_never_opens_the_library_database(monkeypatch):
    from re0 import db

    def refuse(*args, **kwargs):
        raise AssertionError("the MCP surface must not open the library database")

    monkeypatch.setattr(db.Database, "__init__", refuse)
    (answer,) = exchange([paper_search_call()], ResearchTools(None, httpx.MockTransport(lambda request: httpx.Response(200, content=ATOM))))
    assert answer["result"]["isError"] is False


def test_provider_failures_do_not_relay_the_upstream_body():
    def transport(request):
        return httpx.Response(500, content=b"<html>upstream detail that must not travel</html>")
    (answer,) = exchange([paper_search_call()], ResearchTools(None, httpx.MockTransport(transport)))
    text = answer["result"]["content"][0]["text"]
    assert answer["result"]["isError"] is True
    assert "upstream detail" not in text and "Traceback" not in text and "HTTP 500" in text


def test_empty_results_are_reported_as_empty_not_as_absence():
    def transport(request):
        return httpx.Response(200, content=b'<feed xmlns="http://www.w3.org/2005/Atom"></feed>')
    (answer,) = exchange([paper_search_call()], ResearchTools(None, httpx.MockTransport(transport)))
    text = answer["result"]["content"][0]["text"]
    assert answer["result"]["isError"] is False and "documents: 0" in text
    assert "not evidence that the work does not exist" in text

class StubTools:
    """Returns a fixed payload, so the mapping to the versioned structure is what is under test."""

    web_enabled = False

    def __init__(self, payload):
        self.payload = payload

    def execute(self, name, arguments):
        return self.payload


REVEAL = {
    "sources_queried": ["openalex"],
    "source_counts": {"openalex": 1},
    "source_failures": [{"source": "arxiv", "error": "提供商返回 HTTP 406，本次未完成验证"}],
    "documents": [{
        "source_url": "https://arxiv.org/abs/2605.11818",
        "kind": "paper",
        "locator": "metadata from openalex",
        "content": "x" * 9000,
        "paper": {"title": "RevealLayer", "authors": ["A"], "year": 2026},
        "publication": {"state": "preprint", "label": "仅见预印本版本", "venue": "arXiv",
                        "source": "openalex"},
        "institutions": ["清华大学"],
        "artifact_candidates": [{"url": "https://github.com/360CVGroup/RevealLayer",
                                 "origin": "GitHub 名称检索·标识名与项目名一致"}],
        "artifact_search": "searched",
        "some_future_field": {"kept": True},
    }],
}


def test_candidates_and_a_failed_source_survive_the_mcp_output():
    """The acceptance case for #4: a document carrying `artifact_candidates` and a name-search or
    source failure must still carry both after a trip through the tool surface. Losing them made a
    working capability look absent, and a silently dropped failure reads as an empty result."""
    (answer,) = exchange([paper_search_call()], StubTools(REVEAL))
    result = answer["result"]
    text = result["content"][0]["text"]
    structure = result["structuredContent"]
    document = structure["documents"][0]

    assert document["artifact_candidates"][0]["url"] == "https://github.com/360CVGroup/RevealLayer"
    assert document["artifact_search"] == "searched"
    assert structure["coverage"]["source_failures"][0]["source"] == "arxiv"
    # And the summary a reader actually sees must not omit them either.
    assert "github.com/360CVGroup/RevealLayer" in text
    assert "source failed: arxiv" in text and "not 'not found'" in text


def test_a_field_this_version_does_not_describe_is_carried_not_dropped():
    """A schema may evolve; a consumer must never be made to see less than the tool returned."""
    from re0 import result_model
    structure = result_model.normalize(REVEAL)
    assert structure["documents"][0]["unrecognised"] == {"some_future_field": {"kept": True}}
    assert result_model.unknown_field_names(structure) == ["some_future_field"]
    assert "some_future_field" in mcp_server.render(structure)


def test_truncation_is_stated_rather_than_implied():
    """A short body and a cut body must not look alike, in either direction."""
    from re0 import result_model
    long_body = result_model.normalize(REVEAL)
    body = long_body["documents"][0]["body"]
    assert body["content_chars"] == 9000 and body["truncated"] is True
    assert long_body["truncation"]["bodies_excerpted"] == 1

    short = result_model.normalize({"documents": [{"content": "short body"}]})
    assert short["documents"][0]["body"]["truncated"] is False
    assert short["truncation"]["bodies_excerpted"] == 0
    assert "not a cut one" not in short["truncation"]["note"]


def test_the_summary_is_rendered_from_the_structured_result():
    """One structure, two adapters: the text cannot contradict the data behind it."""
    from re0 import result_model
    structure = result_model.normalize(REVEAL)
    text = mcp_server.render(structure)
    assert structure["schema_version"] == result_model.SCHEMA_VERSION
    assert set(structure) >= {"schema_version", "coverage", "documents", "truncation"}
    assert "仅见预印本版本" in text
    assert "institutions: 清华大学" in text
    # The summary stays bounded even though the structure carries more.
    assert len(text) < 4000 and mcp_server.STRUCTURED_BODY_CHARS > mcp_server.DOCUMENT_EXCERPT_CHARS


def test_the_cli_json_and_the_mcp_structured_content_are_the_same_shape():
    """Both machine-readable exits call this one function, so neither can describe the same call
    differently."""
    from re0 import result_model, skill_search
    assert mcp_server.result_model is result_model
    assert "result_model.normalize(result)" in pathlib.Path(skill_search.__file__).read_text(
        encoding="utf-8")
