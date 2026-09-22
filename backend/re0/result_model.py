"""The versioned result shape every entry point shares.

The CLI, the MCP server and a Re0 task all consume the same tool results, so none of them should
decide on its own which fields matter. This module decides once, and the printers become adapters
over it: `mcp_server.render()` reads this structure rather than the raw payload, so a summary can
no longer disagree with the data behind it.

Two rules it exists to enforce:

* **Nothing is dropped silently.** Fields this version does not describe are carried under
  `unrecognised`, so a tool that starts returning something new looks richer rather than emptier.
* **Truncation is stated.** A bounded body carries `content_chars`, `excerpt_chars` and
  `truncated`, so a body that was cut can never be mistaken for a short one.
"""
from __future__ import annotations

SCHEMA_VERSION = "1"
BODY_EXCERPT_CHARS = 4000

PAYLOAD_FIELDS = ("scope", "query", "queries", "note", "sources_queried", "source_counts",
                  "source_failures", "incomplete_results", "duplicates_merged",
                  "dropped_out_of_range", "coverage", "documents", "audit")
# A full-text read is not a retrieval result: it has one source, one version and one parser rather
# than a merged candidate list. It gets its own block in the same versioned structure, so the shape
# stays described instead of a whole feature arriving as `unrecognised`.
FULLTEXT_FIELDS = ("identifier", "source", "version", "state", "detail", "content_type", "parser",
                   "parser_version", "source_url", "final_url", "fetched_at", "bytes_read", "hops",
                   "content_sha256", "resolver_notes", "blocks", "chars", "toc", "attempts",
                   "parse_quality",
                   "chunks", "chunk_count", "slice", "stored", "limitations", "untrusted_note")
DOCUMENT_FIELDS = ("source_url", "kind", "locator", "content", "paper", "publication",
                   "preprint_also", "institutions", "artifact_candidates", "artifact_search",
                   "artifact_search_detail", "artifact_outcome", "resource_audits",
                   "sources", "retrieved_at", "tool", "evidence_ids")


def bounded_body(value, limit: int = BODY_EXCERPT_CHARS) -> dict:
    body = value if isinstance(value, str) else ""
    excerpt = body[:limit]
    return {"excerpt": excerpt, "content_chars": len(body), "excerpt_chars": len(excerpt),
            "truncated": len(excerpt) < len(body)}


def normalize_document(document: dict, *, body_chars: int = BODY_EXCERPT_CHARS) -> dict:
    if not isinstance(document, dict):
        return {"unrecognised": {"document": document}}
    normalized = {key: document[key] for key in DOCUMENT_FIELDS if key in document}
    normalized["body"] = bounded_body(normalized.pop("content", ""), body_chars)
    # Anything newer than this schema version rides along instead of disappearing.
    extra = {key: value for key, value in document.items() if key not in DOCUMENT_FIELDS}
    if extra:
        normalized["unrecognised"] = extra
    return normalized


def normalize(payload: dict, *, body_chars: int = BODY_EXCERPT_CHARS) -> dict:
    """Build the versioned structure from one tool result. Pure: it retrieves and verifies nothing."""
    payload = payload if isinstance(payload, dict) else {}
    documents = [item for item in (payload.get("documents") or []) if isinstance(item, dict)]
    result: dict = {"schema_version": SCHEMA_VERSION}
    for key in ("scope", "query", "note"):
        if payload.get(key) is not None:
            result[key] = payload[key]
    result["coverage"] = {
        "documents": len(documents),
        "sources_queried": list(payload.get("sources_queried") or []),
        "source_counts": dict(payload.get("source_counts") or {}),
        # A failed source is not an absent result, so it travels as a list of its own rather than
        # as a count that could be read either way.
        "source_failures": [dict(item) for item in (payload.get("source_failures") or [])
                            if isinstance(item, dict)],
        "incomplete_results": bool(payload.get("incomplete_results")),
        # How much the merge folded away, and how many records the year window excluded. Both are
        # coverage facts: without them a smaller list has no explanation.
        "duplicates_merged": payload.get("duplicates_merged"),
        "dropped_out_of_range": payload.get("dropped_out_of_range"),
    }
    if payload.get("audit"):
        # Resource-audit coverage sits beside retrieval coverage rather than inside it: one says
        # which services answered, the other says which papers were searched and which candidates
        # were actually checked. Each carries the denominator its counts are out of.
        result["audit"] = payload["audit"]
    result["documents"] = [normalize_document(item, body_chars=body_chars) for item in documents]
    excerpted = sum(1 for item in result["documents"] if item["body"]["truncated"])
    result["truncation"] = {
        "bodies_excerpted": excerpted,
        "excerpt_chars": body_chars,
        "note": ("document bodies are excerpts; content_chars gives the true length, so a short "
                 "body is not a cut one" if excerpted else "no document body was cut"),
    }
    described = set(PAYLOAD_FIELDS)
    if "chunk_count" in payload or "parser_version" in payload:
        result["fulltext"] = {key: payload[key] for key in FULLTEXT_FIELDS if key in payload}
        described |= set(FULLTEXT_FIELDS)
    extra = {key: value for key, value in payload.items() if key not in described}
    if extra:
        result["unrecognised"] = extra
    return result


def unknown_field_names(structure: dict) -> list[str]:
    """Names carried under `unrecognised`, at either level, so a summary can mention them."""
    names = set(structure.get("unrecognised") or {})
    for item in structure.get("documents") or []:
        names.update(item.get("unrecognised") or {})
    return sorted(names)
