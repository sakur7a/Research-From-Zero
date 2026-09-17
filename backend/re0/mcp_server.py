"""Minimal MCP stdio server exposing Re0's read-only research tools.

MCP over stdio is newline-delimited JSON-RPC 2.0, so this needs no dependency and
Re0 keeps its four-package runtime. Tool descriptors come from the same Pydantic
contracts the in-task model sees, so the two surfaces cannot drift apart.

This is a retrieval endpoint, not a Re0 task. It creates no run, no checkpoint and
no evidence ID: results carry a locator and a source URL instead. Re0's citation
discipline ("every finding cites evidence IDs from the same task") applies to its
own runs and is not implied by a result returned here. Task-protocol tools
(`update_plan`, `finish_report`), the task-scoped `read_evidence` and the
consent-gated `search_library` are deliberately not exposed.

Wire it into a client with, for example:

    {"mcpServers": {"re0": {"command": "python", "args": ["-m", "re0.mcp_server"]}}}

Nothing may be written to stdout except protocol messages.
"""
from __future__ import annotations

import json
import sys

from pydantic import ValidationError

from .agent.tools import TOOL_TYPES, ResearchTools
from .providers import ProviderError

SERVER_NAME = "re0-research"
SERVER_VERSION = "0.2.0"
DEFAULT_PROTOCOL_VERSION = "2025-06-18"
DOCUMENT_EXCERPT_CHARS = 1500
TAIL_CHARS = 1200
# Meaningless without a Re0 task, or gated on per-task user consent.
EXCLUDED = frozenset({"update_plan", "finish_report", "read_evidence", "search_library"})
JSON_OBJECT_LIMIT = 4 * 1024 * 1024


def exposed_names(*, web_enabled: bool) -> list[str]:
    """Read-only retrieval tools. `search_web` follows the same env switch as a task."""
    return [name for name in TOOL_TYPES
            if name not in EXCLUDED and (name != "search_web" or web_enabled)]


def catalog(*, web_enabled: bool) -> list[dict]:
    return [{"name": name, "description": TOOL_TYPES[name][1],
             "inputSchema": TOOL_TYPES[name][0].model_json_schema()}
            for name in exposed_names(web_enabled=web_enabled)]


def render(payload: dict) -> str:
    """Bounded, readable text. The caller is an agent, so its context pays for this."""
    lines = []
    for key in ("scope", "query", "note"):
        if payload.get(key):
            lines.append(f"{key}: {payload[key]}")
    documents = payload.get("documents") or []
    lines.append(f"documents: {len(documents)}")
    for index, item in enumerate(documents, start=1):
        paper = item.get("paper") or {}
        lines.append(f"[{index}] {paper.get('title') or item.get('kind') or 'source'}")
        lines.append(f"    locator: {item.get('locator', '')}")
        lines.append(f"    source:  {item.get('source_url', '')}")
        if paper:
            small = {key: paper.get(key) for key in ("authors", "year", "doi", "arxiv_id") if paper.get(key)}
            lines.append("    paper:   " + json.dumps(small, ensure_ascii=False))
        body = item.get("content") or ""
        excerpt = body[:DOCUMENT_EXCERPT_CHARS]
        lines.append(f"    excerpt ({len(excerpt)}/{len(body)} chars): {excerpt}")
    if not documents:
        lines.append("no source material returned; rephrase the query. "
                     "An empty result is not evidence that the work does not exist.")
    rest = {key: value for key, value in payload.items() if key not in {"documents", "scope", "query", "note"}}
    if rest:
        tail = json.dumps(rest, ensure_ascii=False)
        lines.append("rest: " + tail[:TAIL_CHARS] + ("…" if len(tail) > TAIL_CHARS else ""))
    return "\n".join(lines)


def call_tool(tools: ResearchTools, name: str, arguments: dict, *, web_enabled: bool) -> tuple[str, bool]:
    if name not in exposed_names(web_enabled=web_enabled):
        return f"Re0 does not expose this tool here: {name}", True
    try:
        return render(tools.execute(name, arguments or {})), False
    except ValidationError as exc:
        # Report which fields are wrong, not a pydantic traceback.
        fields = [".".join(map(str, error["loc"])) for error in exc.errors()][:10]
        return "arguments rejected: " + ", ".join(fields), True
    except ProviderError as exc:
        return f"source service failed: {exc}", True
    except ValueError as exc:
        return f"arguments or authorization rejected: {exc}", True
    except Exception:
        # Provider responses are not exception text; never relay a raw traceback.
        return "retrieval failed: check the query and arguments, then retry.", True


def _reply(identifier, result: dict) -> dict:
    return {"jsonrpc": "2.0", "id": identifier, "result": result}


def handle(message: dict, tools: ResearchTools, *, web_enabled: bool):
    """One JSON-RPC message in, one response out (or None for notifications)."""
    method, identifier = message.get("method"), message.get("id")
    params = message.get("params") or {}
    if identifier is None:
        return None  # notification such as `notifications/initialized`: tolerated, no reply
    if method == "initialize":
        # Echo the client's version so a newer client still gets a handshake it accepts.
        return _reply(identifier, {"protocolVersion": params.get("protocolVersion") or DEFAULT_PROTOCOL_VERSION,
                                   "capabilities": {"tools": {}},
                                   "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION}})
    if method == "ping":
        return _reply(identifier, {})
    if method == "tools/list":
        return _reply(identifier, {"tools": catalog(web_enabled=web_enabled)})
    if method == "tools/call":
        text, failed = call_tool(tools, params.get("name") or "", params.get("arguments") or {}, web_enabled=web_enabled)
        return _reply(identifier, {"content": [{"type": "text", "text": text}], "isError": failed})
    return {"jsonrpc": "2.0", "id": identifier,
            "error": {"code": -32601, "message": f"Unsupported method: {method}"}}


def serve(stdin=None, stdout=None, tools: ResearchTools | None = None) -> None:
    """Loop until stdin ends. `tools` is injectable so tests can supply a transport."""
    stdin = sys.stdin if stdin is None else stdin
    stdout = sys.stdout if stdout is None else stdout
    tools = ResearchTools(None) if tools is None else tools
    for line in stdin:
        line = line.strip()
        if not line or len(line) > JSON_OBJECT_LIMIT:
            continue
        try:
            message = json.loads(line)
        except ValueError:
            continue
        if not isinstance(message, dict):
            continue
        answer = handle(message, tools, web_enabled=tools.web_enabled)
        if answer is not None:
            stdout.write(json.dumps(answer, ensure_ascii=False) + "\n")
            stdout.flush()


def main() -> None:
    serve()


if __name__ == "__main__":
    main()
