"""`re0 paper text`: read one open-access paper and print what the read established.

Thin on purpose. The reading, the locators and the state vocabulary live in `re0.fulltext`; the
rendering is shared with the MCP surface, so the CLI and a machine consumer cannot describe the same
read differently.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .fulltext import read_fulltext
from .workspace import Workspace, WorkspaceError

EPILOG = """\
states: ok, no_fulltext, not_found, access_required, rate_limited, too_large, scan_only,
        parser_missing, not_allowed, fetch_failed. None of the failures means the paper says
        nothing about your question; they describe this read.

The whole text is never printed. One bounded slice comes back, the rest are listed by locator;
pass --locator to read another. With --workspace the chunks are stored there and keep stable ids,
so a follow-up reads a slice instead of fetching the paper again.
"""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="re0 paper text", description="Read an open-access paper's full text, with locators.",
        epilog=EPILOG, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("identifier",
                        help="arXiv ID (2312.00286v1) or ACL Anthology ID (2024.acl-long.1, "
                             "P18-1001). A DOI is refused: resolving one leads to a publisher that "
                             "may be paywalled, and this command routes around no paywall.")
    parser.add_argument("--locator", default="", metavar="LOC",
                        help="which chunk to print, e.g. 'p.4' or a section-and-paragraph locator. "
                             "Empty means the first one.")
    parser.add_argument("--slice-chars", type=int, default=4000, metavar="N",
                        help="ceiling on the printed slice, default 4000. It bounds what is shown, "
                             "not what was read.")
    parser.add_argument("--toc", action="store_true",
                        help="print the table of contents and the chunk locators, and no text")
    parser.add_argument("--workspace", default=None, metavar="DIR",
                        help="store the chunks in this directory and print their source ids. "
                             "Opt-in: omitted, nothing is written anywhere.")
    parser.add_argument("--json", dest="json_path", default=None, metavar="PATH",
                        help="write the versioned result structure here (the same shape the MCP "
                             "surface returns)")
    return parser


def main(argv=None) -> int:
    parser = build_parser()
    arguments = parser.parse_args(sys.argv[1:] if argv is None else argv)
    workspace = None
    if arguments.workspace:
        try:
            workspace = Workspace(arguments.workspace).open()
            print(f"workspace {workspace.workspace_id} at {workspace.root}", file=sys.stderr)
        except WorkspaceError as exc:
            print(f"workspace refused: {exc}", file=sys.stderr)
            return 2
    result = read_fulltext(arguments.identifier, workspace=workspace, locator=arguments.locator,
                           slice_chars=arguments.slice_chars)
    from .mcp_server import render_fulltext
    if arguments.toc:
        result = {**result, "slice": None}
    print(render_fulltext(result))
    for note in result.get("limitations") or []:
        print(f"  - {note}")
    if result.get("state") != "ok":
        print("\nthe full text was not read. That is a statement about this read, not about the "
              "paper: it does not mean the paper is silent on anything.", file=sys.stderr)
    if arguments.json_path:
        from . import result_model
        structure = result_model.normalize(result)
        Path(arguments.json_path).write_text(
            json.dumps(structure, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\nfull result written to {arguments.json_path} "
              f"(schema_version {structure['schema_version']}; the read is under `fulltext`)")
    return 0 if result.get("state") == "ok" else 4
