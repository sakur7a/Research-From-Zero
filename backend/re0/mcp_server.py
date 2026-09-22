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

from . import result_model
from .agent.tools import TOOL_TYPES, ResearchTools
from .providers import ProviderError

SERVER_NAME = "re0-research"
SERVER_VERSION = "0.2.0"
DEFAULT_PROTOCOL_VERSION = "2025-06-18"
# The summary is tighter than the structured body: an agent's context pays for this text, while
# a client that wants more can read `structuredContent`.
DOCUMENT_EXCERPT_CHARS = 1500
STRUCTURED_BODY_CHARS = 4000
ARTIFACT_LINES = 3
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


def render(structure: dict) -> str:
    """Readable text, built from the structured result rather than from the raw payload.

    Bounded on purpose, because the caller is an agent. Everything the summary leaves out is still
    in `structuredContent`, and the summary says when that is the case instead of looking complete.
    """
    lines = []
    for key in ("scope", "query", "note"):
        if structure.get(key):
            lines.append(f"{key}: {structure[key]}")
    coverage = structure.get("coverage") or {}
    documents = structure.get("documents") or []
    lines.append(f"documents: {len(documents)}")
    for failure in coverage.get("source_failures") or []:
        # Printed before the documents: a failed source is not an empty one, and a reader who stops
        # at the list would otherwise read the gap as absence.
        lines.append(f"source failed: {failure.get('source')}: {failure.get('error')} "
                     "(not searched; this is not 'not found')")
    for index, item in enumerate(documents, start=1):
        paper = item.get("paper") or {}
        lines.append(f"[{index}] {paper.get('title') or item.get('kind') or 'source'}")
        lines.append(f"    locator: {item.get('locator', '')}")
        lines.append(f"    source:  {item.get('source_url', '')}")
        if paper:
            small = {key: paper.get(key) for key in ("authors", "year", "doi", "arxiv_id") if paper.get(key)}
            lines.append("    paper:   " + json.dumps(small, ensure_ascii=False))
        publication = item.get("publication") or {}
        if publication.get("state"):
            described = publication.get("label") or publication["state"]
            if publication.get("venue"):
                described += f" - {publication['venue']}"
            lines.append(f"    publication: {described}")
        if item.get("preprint_also"):
            lines.append("    publication note: a preprint version is also indexed")
        institutions = item.get("institutions") or []
        if institutions:
            shown = ", ".join(str(name) for name in institutions[:3])
            lines.append("    institutions: " + shown
                         + (f" (+{len(institutions) - 3} more)" if len(institutions) > 3 else ""))
        candidates = item.get("artifact_candidates") or []
        for candidate in candidates[:ARTIFACT_LINES]:
            origin = f" [{candidate['origin']}]" if candidate.get("origin") else ""
            lines.append(f"    artifact candidate (name match, unverified): {candidate.get('url')}{origin}")
        if len(candidates) > ARTIFACT_LINES:
            lines.append(f"    (+{len(candidates) - ARTIFACT_LINES} more artifact candidates)")
        if not candidates:
            # Stated even when empty, and the search state is named, so silence is not read as
            # "there is no released code".
            lines.append("    artifact candidates: none "
                         f"(name search: {item.get('artifact_search') or 'not-run'})")
        body = item.get("body") or {}
        full = body.get("excerpt") or ""
        excerpt = full[:DOCUMENT_EXCERPT_CHARS]
        cut = body.get("truncated") or len(excerpt) < len(full)
        lines.append(f"    excerpt ({len(excerpt)}/{body.get('content_chars', 0)} chars"
                     + (", truncated" if cut else "") + "): " + excerpt)
    if not documents:
        lines.append("no source material returned; rephrase the query. "
                     "An empty result is not evidence that the work does not exist.")
    unknown = result_model.unknown_field_names(structure)
    if unknown:
        # The presence of fields this version does not describe is itself information.
        lines.append("carried in structuredContent but not described by this schema version: "
                     + ", ".join(unknown))
    return "\n".join(lines)


def call_tool(tools: ResearchTools, name: str, arguments: dict, *,
              web_enabled: bool) -> tuple[str, dict | None, bool]:
    """Return `(summary_text, structured_content, failed)`.

    The structured form is the same object the summary is rendered from, so a consumer reading one
    cannot be told something the other contradicts.
    """
    if name not in exposed_names(web_enabled=web_enabled):
        return f"Re0 does not expose this tool here: {name}", None, True
    try:
        structure = result_model.normalize(tools.execute(name, arguments or {}),
                                           body_chars=STRUCTURED_BODY_CHARS)
    except ValidationError as exc:
        # Report which fields are wrong, not a pydantic traceback.
        fields = [".".join(map(str, error["loc"])) for error in exc.errors()][:10]
        return "arguments rejected: " + ", ".join(fields), None, True
    except ProviderError as exc:
        return f"source service failed: {exc}", None, True
    except ValueError as exc:
        return f"arguments or authorization rejected: {exc}", None, True
    except Exception:
        # Provider responses are not exception text; never relay a raw traceback.
        return "retrieval failed: check the query and arguments, then retry.", None, True
    return render(structure), structure, False


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
        text, structured, failed = call_tool(tools, params.get("name") or "",
                                             params.get("arguments") or {}, web_enabled=web_enabled)
        result = {"content": [{"type": "text", "text": text}], "isError": failed}
        if structured is not None:
            # The machine-readable form travels beside the summary, so no field survives only if
            # the summary happened to mention it.
            result["structuredContent"] = structured
        return _reply(identifier, result)
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
