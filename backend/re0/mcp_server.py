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
from .workspace import Workspace, WorkspaceError

SERVER_NAME = "re0-research"
SERVER_VERSION = "0.2.0"
DEFAULT_PROTOCOL_VERSION = "2025-06-18"
# Versions this server actually implements. A request for anything else is answered with a version
# we do support, never echoed back: agreeing to a protocol we have not implemented is a promise the
# rest of this file cannot keep.
SUPPORTED_PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26")
# The summary is tighter than the structured body: an agent's context pays for this text, while
# a client that wants more can read `structuredContent`.
DOCUMENT_EXCERPT_CHARS = 1500
STRUCTURED_BODY_CHARS = 4000
ARTIFACT_LINES = 3
# An audit row is longer than a candidate line, so fewer of them are worth an agent's context.
AUDIT_LINES = 2
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


def audit_lines(item: dict) -> list:
    """The field-level audit, bounded.

    A summary that names a state without naming what the state is about invites exactly the
    misreading the vocabulary exists to prevent, so each line keeps the provider's own status, the
    depth, and the classes that were *not* resolved.
    """
    audits = item.get("resource_audits") or []
    lines = []
    for audit in audits[:AUDIT_LINES]:
        coverage = audit.get("coverage") or {}
        states = {name: (finding or {}).get("state", "unknown") for name, finding in coverage.items()}
        groups = {}
        for name, state in states.items():
            groups.setdefault(state, []).append(name)
        lines.append(f"    resource audit: {audit.get('status')}"
                     + (f" (provider status {audit['provider_status']})"
                        if audit.get("provider_status") else "")
                     + f" · depth {audit.get('verification_depth')}"
                     + f" · access {audit.get('access')} · {audit.get('resource_url', '')}")
        for state in ("present", "absent_in_scope", "requires_access", "unknown", "check_failed",
                      "not_applicable"):
            if groups.get(state):
                # "present" is a filename candidate; saying so here costs one clause and stops the
                # line reading as a working artifact.
                note = " (filename candidates, not verified)" if state == "present" else ""
                lines.append(f"      {state}{note}: " + ", ".join(sorted(groups[state])))
        attribution = audit.get("attribution", "unconfirmed")
        extras = [f"attribution {attribution}", f"version_match {audit.get('version_match', 'unknown')}"]
        licences = audit.get("licences") or {}
        if licences:
            extras.append("licence declared: "
                          + ", ".join(f"{name}={value}" for name, value in licences.items()))
        if audit.get("author_declaration") != "undeclared":
            extras.append(f"author declaration: {audit['author_declaration']}")
        lines.append("      " + " · ".join(extras))
        for limitation in (audit.get("limitations") or [])[:1]:
            lines.append(f"      limit: {limitation}")
    if len(audits) > AUDIT_LINES:
        lines.append(f"    (+{len(audits) - AUDIT_LINES} more resource audits in structuredContent)")
    return lines


def render_fulltext(text: dict) -> str:
    """A full-text read rendered from the same structure the JSON carries.

    It states what was read, what was not, and why, before any of the text: a slice that arrives
    without its state reads as the whole paper.
    """
    lines = [f"full text: {text.get('identifier', '')} [{text.get('state', '')}]"]
    lines.append(f"    version: {text.get('version', '')}  parser: {text.get('parser', '')} "
                 f"(parser_version {text.get('parser_version', '')})")
    lines.append(f"    from: {text.get('final_url') or text.get('source_url', '')}")
    lines.append(f"    fetched_at: {text.get('fetched_at', '')}  bytes: {text.get('bytes_read', 0)}  "
                 f"sha256: {str(text.get('content_sha256', ''))[:16]}")
    if text.get("detail"):
        lines.append(f"    detail: {text['detail']}")
    lines.append(f"    blocks: {text.get('blocks', 0)}  chars: {text.get('chars', 0)}  "
                 f"parse_quality: {text.get('parse_quality', '')}")
    for attempt in (text.get("attempts") or [])[:4]:
        lines.append(f"    attempt: {attempt.get('kind')} {attempt.get('url')} -> "
                     f"{attempt.get('state')}" + (f" ({attempt.get('detail')})"
                                                  if attempt.get("detail") else ""))
    for entry in (text.get("toc") or [])[:12]:
        lines.append(f"    toc: {entry.get('locator')} {entry.get('title')}"
                     + ("  [back matter]" if entry.get("back_matter") else ""))
    chunks = text.get("chunks") or []
    if chunks:
        lines.append(f"    chunks: {text.get('chunk_count', len(chunks))} "
                     f"(first {min(len(chunks), 8)} locators: "
                     + ", ".join(str(chunk.get("locator")) for chunk in chunks[:8]) + ")")
    slice_ = text.get("slice")
    if slice_:
        lines.append(f"    slice {slice_.get('locator')} ({slice_.get('chars')} chars"
                     + (", truncated" if slice_.get("truncated") else "") + "):")
        lines.append("      " + str(slice_.get("text", "")).replace("\n", "\n      "))
    stored = text.get("stored") or {}
    if stored.get("chunks"):
        lines.append(f"    stored: {stored['chunks']} chunks in workspace "
                     f"{stored.get('workspace_id', '')}")
    for note in (text.get("limitations") or [])[:6]:
        lines.append(f"    limit: {note}")
    if text.get("untrusted_note"):
        lines.append(f"    untrusted: {text['untrusted_note']}")
    return "\n".join(lines)


def render(structure: dict) -> str:
    """Readable text, built from the structured result rather than from the raw payload.

    Bounded on purpose, because the caller is an agent. Everything the summary leaves out is still
    in `structuredContent`, and the summary says when that is the case instead of looking complete.
    """
    if structure.get("fulltext"):
        return render_fulltext(structure["fulltext"])
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
        detail = item.get("artifact_search_detail") or {}
        for failure in (detail.get("failures") or [])[:ARTIFACT_LINES]:
            # A name search that did not complete is not a search that found nothing.
            lines.append(f"    name search did not complete: {failure}")
        if detail.get("reason_not_run"):
            lines.append(f"    name search not run: {detail['reason_not_run']}")
        lines.extend(audit_lines(item))
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


def call_tool(tools: ResearchTools, name: str, arguments: dict, *, web_enabled: bool,
              workspace: Workspace | None = None) -> tuple[str, dict | None, bool]:
    """Return `(summary_text, structured_content, failed)`.

    The structured form is the same object the summary is rendered from, so a consumer reading one
    cannot be told something the other contradicts. When a workspace is open, each document is also
    recorded there and its stable source id travels in the structured content.
    """
    if name not in exposed_names(web_enabled=web_enabled):
        return f"Re0 does not expose this tool here: {name}", None, True
    try:
        payload = tools.execute(name, arguments or {}, workspace=workspace)
        structure = result_model.normalize(payload, body_chars=STRUCTURED_BODY_CHARS)
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
    if workspace is not None:
        # Best effort per document: a source that cannot be stored must not turn a successful
        # retrieval into a failed call, and the refusal is reported rather than swallowed.
        raw_documents = [item for item in (payload.get("documents") or []) if isinstance(item, dict)]
        for document, normalized in zip(raw_documents, structure["documents"]):
            try:
                normalized["source_id"] = workspace.record(document, tool=name)
            except WorkspaceError as exc:
                normalized["source_id_refused"] = str(exc)
    return render(structure), structure, False


class Session:
    """Protocol state for one connection, plus the workspace when the caller named one."""

    def __init__(self, workspace: Workspace | None = None):
        self.initialized = False
        self.negotiated = ""
        self.workspace = workspace


def _reply(identifier, result: dict) -> dict:
    return {"jsonrpc": "2.0", "id": identifier, "result": result}


def _error(identifier, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": identifier, "error": {"code": code, "message": message}}


def handle(message: dict, tools: ResearchTools, *, web_enabled: bool,
           session: Session | None = None):
    """One JSON-RPC message in, one response out (or None for notifications).

    Every branch validates rather than assumes: a request that is not an object, a `params` that is
    not an object, and a `tools/call` without a name all get a defined error instead of an
    exception. Nothing raises out of here, because a raise would end the loop and take the
    connection with it.
    """
    session = session if session is not None else Session()
    if not isinstance(message, dict):
        return _error(None, -32600, "a request must be an object")
    method, identifier = message.get("method"), message.get("id")
    params = message.get("params")
    params = {} if params is None else params
    if not isinstance(params, dict):
        return _error(identifier, -32602, "params must be an object")
    if message.get("jsonrpc") != "2.0":
        return _error(identifier, -32600, "jsonrpc must be \'2.0\'")
    if not isinstance(method, str) or not method:
        return _error(identifier, -32600, "method must be a non-empty string")
    if identifier is None:
        return None  # notification such as `notifications/initialized`: tolerated, no reply
    if method == "initialize":
        requested = params.get("protocolVersion")
        # A version we implement is echoed; anything else gets ours, so the client can decide
        # whether to continue rather than us agreeing to a protocol we have not built.
        chosen = requested if requested in SUPPORTED_PROTOCOL_VERSIONS else DEFAULT_PROTOCOL_VERSION
        session.initialized, session.negotiated = True, chosen
        return _reply(identifier, {"protocolVersion": chosen,
                                   "capabilities": {"tools": {}},
                                   "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION}})
    if method == "ping":
        return _reply(identifier, {})
    if method in ("tools/list", "tools/call") and not session.initialized:
        # The handshake is state, not decoration: a tool request before it is refused rather than
        # answered under assumptions the client never agreed to.
        return _error(identifier, -32002, "initialize before using tools")
    if method == "tools/list":
        return _reply(identifier, {"tools": catalog(web_enabled=web_enabled)})
    if method == "tools/call":
        name, arguments = params.get("name"), params.get("arguments", {})
        if not isinstance(name, str) or not name:
            return _error(identifier, -32602, "tools/call needs a string name")
        if not isinstance(arguments, dict):
            return _error(identifier, -32602, "arguments must be an object")
        text, structured, failed = call_tool(tools, name, arguments, web_enabled=web_enabled,
                                             workspace=session.workspace)
        result = {"content": [{"type": "text", "text": text}], "isError": failed}
        if structured is not None:
            # The machine-readable form travels beside the summary, so no field survives only if
            # the summary happened to mention it.
            result["structuredContent"] = structured
        return _reply(identifier, result)
    return _error(identifier, -32601, f"Unsupported method: {method}")


def serve(stdin=None, stdout=None, tools: ResearchTools | None = None,
          workspace: Workspace | None = None) -> None:
    """Loop until stdin ends. `tools` is injectable so tests can supply a transport.

    `workspace` is None by default, and that default is the point: no directory is opened, no
    database is touched, and nothing is written unless the caller named one.
    """
    stdin = sys.stdin if stdin is None else stdin
    stdout = sys.stdout if stdout is None else stdout
    tools = ResearchTools(None) if tools is None else tools
    session = Session(workspace)
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
        answer = handle(message, tools, web_enabled=tools.web_enabled, session=session)
        if answer is not None:
            stdout.write(json.dumps(answer, ensure_ascii=False) + "\n")
            stdout.flush()


def main(argv=None) -> None:
    import argparse
    parser = argparse.ArgumentParser(description="Re0's read-only tools over stdio.")
    parser.add_argument("--workspace", default=None, metavar="DIR",
                        help="record retrieved sources in this directory and give them stable ids. "
                             "Omitted by default: the surface stays stateless and opens nothing")
    options = parser.parse_args(sys.argv[1:] if argv is None else argv)
    workspace = None
    if options.workspace:
        workspace = Workspace(options.workspace).open()
        print(f"workspace {workspace.workspace_id} at {workspace.root}", file=sys.stderr)
    serve(workspace=workspace)


if __name__ == "__main__":
    main()
