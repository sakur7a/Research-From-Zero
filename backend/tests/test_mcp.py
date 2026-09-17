"""Protocol tests for the MCP stdio surface.

These drive the server in-process, so verifying it needs no MCP dependency. The
handshake was additionally checked against the official `mcp` client during
development (see docs/TESTING.md); that check is not part of this suite because it
would pull a large dependency tree into the test extra.
"""
import io
import json

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
