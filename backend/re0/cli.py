"""Thin console entry points.

Retrieval, reporting, the MCP server and the model probe already exist as functions; this module
only dispatches to them, so no retrieval logic is duplicated here. The skill wrapper
(`skills/re0-paper-search/scripts/paper_search.py`) calls the same search entry point.

Two capabilities are kept apart on purpose, and `doctor` says which is which:

* **No model needed** — `paper search` (five scholarly sources) and `mcp` (local stdio tools).
  These work with no key at all.
* **Needs a model key (BYOK)** — a standalone Re0 task that plans and calls tools. This module
  never starts an LLM of its own: calling a command here does not nest a second agent, and
  host-tool mode (an agent calls the MCP tools) stays distinct from standalone mode.

`doctor` performs no network or paid request unless `--probe-network` is passed explicitly, so a
routine check cannot spend money or trip a provider's rate limit.
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

PROGRAM = "re0"
# Names only, so `doctor` can say what is configured without ever reading a value out.
CREDENTIAL_NAMES = ("RE0_LLM_BASE_URL", "RE0_LLM_MODEL", "RE0_LLM_API_KEY", "RE0_LLM_TOKEN_PARAMETER",
                    "RE0_ENV_FILE", "RE0_ENV_INCLUDE_AGENT_DIRS", "RE0_HOME",
                    "SEMANTIC_SCHOLAR_API_KEY", "SEMANTICSCHOLAR_API_KEY", "OPENALEX_MAILTO",
                    "OPENREVIEW_TOKEN", "TAVILY_API_KEY", "GITHUB_TOKEN")
PROBE_HOSTS = ("export.arxiv.org", "api.github.com")


def version() -> str:
    try:
        from importlib.metadata import version as package_version
        return package_version("re0-research")
    except Exception:  # noqa: BLE001 - running from a checkout without installed metadata
        return "unknown (not installed as a distribution)"


def doctor(probe_network: bool = False) -> int:
    """Report what can run now, without a model and without spending anything."""
    print(f"Re0 doctor — re0-research {version()}")
    print(f"python: {sys.version.split()[0]} (this package needs >=3.11)")
    print(f"package: {Path(__file__).resolve().parent}")
    print()
    print("mode: `paper search` and `mcp` need no model key; a standalone task does")
    print("      this CLI never starts an LLM of its own, so no command nests a second agent")
    print()
    print("credentials (names only; values are never read out):")
    for name in CREDENTIAL_NAMES:
        print(f"  {name}: {'set' if os.environ.get(name) else 'not set'}")
    try:
        from re0.skill_search import load_credentials
        print(f"  credential file: {load_credentials()}")
    except Exception as exc:  # noqa: BLE001 - reported, never a traceback
        print(f"  credential file: could not be resolved ({type(exc).__name__})")
    print()
    try:
        from re0.agent.tools import TOOL_TYPES
        print(f"tools available to a task: {len(TOOL_TYPES)} ({', '.join(sorted(TOOL_TYPES))})")
    except Exception as exc:  # noqa: BLE001
        print(f"tools: could not be loaded ({type(exc).__name__}); the install looks incomplete")
        return 2
    print()
    if not probe_network:
        print("network probes: not run. Retrieval and resource checks do reach the internet, so they "
              "only run when asked: `re0 doctor --probe-network` spends requests and may hit a "
              "provider's rate limit.")
        return 0
    print("network probes: running because --probe-network was passed")
    from re0.providers import ProviderError, check_resource
    for host in PROBE_HOSTS:
        url = ("https://export.arxiv.org/api/query?search_query=all:test&max_results=1"
               if host == "export.arxiv.org" else "https://api.github.com/rate_limit")
        try:
            observation = check_resource(url)
            print(f"  {host}: {observation.status}")
        except ProviderError as exc:
            print(f"  {host}: unavailable ({exc})")
        except Exception as exc:  # noqa: BLE001 - never a traceback, never a key
            print(f"  {host}: unavailable ({type(exc).__name__})")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog=PROGRAM, description="Re0 research entry points.")
    parser.add_argument("--version", action="version", version=f"{PROGRAM} {version()}")
    subcommands = parser.add_subparsers(dest="command")

    paper = subcommands.add_parser("paper", help="literature retrieval (no model key needed)")
    paper_actions = paper.add_subparsers(dest="action")
    paper_actions.add_parser("search", help="search five scholarly sources and merge them")

    doctor_parser = subcommands.add_parser("doctor", help="report what can run without spending")
    doctor_parser.add_argument("--probe-network", action="store_true",
                               help="also run the network probes (spends requests; never implied)")

    subcommands.add_parser("mcp", help="serve the read-only tools over stdio (no model key needed)")
    return parser


def main(argv=None) -> int:
    """Dispatch, passing the search flags through untouched.

    The passthrough is split off *before* argparse runs, so `re0 paper search --help` reaches the
    skill's own parser and prints the real flags rather than a second, thinner copy of them.
    """
    argv = list(sys.argv[1:] if argv is None else argv)
    passthrough: list = []
    if argv[:2] == ["paper", "search"]:
        argv, passthrough = ["paper", "search"], argv[2:]
    parser = build_parser()
    if not argv:
        parser.print_help()
        return 2
    try:
        arguments = parser.parse_args(argv)
    except SystemExit as exc:
        # argparse exits for --help and for bad input after printing to stderr. Returning the
        # status keeps `main()` total for callers that embed it.
        return int(exc.code or 0)
    if arguments.command == "doctor":
        return doctor(probe_network=arguments.probe_network)
    if arguments.command == "paper" and arguments.action == "search":
        from re0.skill_search import main as search_main
        return search_main(passthrough)
    if arguments.command == "mcp":
        from re0.mcp_server import main as mcp_main
        mcp_main()
        return 0
    parser.print_help()
    return 2
