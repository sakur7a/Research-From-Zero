"""Thin console entry points.

Retrieval, reporting, the MCP server and the model probe already exist as functions; this module
only dispatches to them, so no retrieval logic is duplicated here. The skill wrapper
(`skills/re0-paper-search/scripts/paper_search.py`) calls the same search entry point.

Two capabilities are kept apart on purpose, and `doctor` says which is which:

* **No model needed** — `paper search` (five scholarly sources), `paper text` (one open-access
  paper), `mcp` (local stdio tools), `skill` (package or install the skill directory), `zotero`
  (read-only incremental sync from one library) and the read-only half of `session` (`list`, `show`,
  `delta`, `scope`). These work with no model key at all — `zotero` needs its own `ZOTERO_API_KEY`
  only for the commands that read the remote library.
* **Needs a model key (BYOK)** — a standalone Re0 task that plans and calls tools, and therefore
  `session follow-up` / `session retry`, which start one. This module never starts an LLM of its
  own: those two post to the running local service, where the key already lives in memory, so
  calling a command here does not nest a second agent, and host-tool mode (an agent calls the MCP
  tools) stays distinct from standalone mode.

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


def print_deployment() -> bool:
    """Say which mode this process would run in, and whether it could start at all.

    Everything else doctor reports is about capability; this is about exposure, so it comes first.
    The session secret is named as present or absent and never read out. Returns False when the
    declared mode could not start, so a script checking the exit status is not told "all clear".

    The quotas are printed here because a ceiling nobody can see is a mystery to the person it just
    refused, and an operator reading `re0 doctor` is the first one they will ask.
    """
    from re0.deployment import DeploymentError, from_env
    from re0.quota import quota_limits_from_env

    deployment = from_env()
    declared = bool(os.getenv("RE0_MODE", "").strip())
    print(f"deployment: {deployment.mode}"
          + ("" if declared else "（RE0_MODE 未声明，按本地单人模式运行）"))
    try:
        deployment.require_startable()
    except DeploymentError as exc:
        for line in str(exc).splitlines():
            print(f"            {line}")
        return False
    try:
        limits = quota_limits_from_env(os.environ, hosted=deployment.hosted)
    except ValueError as exc:
        # `create_app` raises the same thing, so the service really would not start on this number.
        print(f"            配额配置无效：{exc}")
        return False
    if deployment.hosted:
        print(f"            对外入口 {deployment.public_entry}；"
              f"允许的来源 {len(deployment.allowed_origins)} 个")
        print("            每个 /api/* 接口都需要登录会话；账户用 `python -m re0 auth create-user` 开")
    else:
        print("            单人本地模式：没有账户也没有登录这一步，只应绑回环地址；")
        print("            run.py 会拒绝非回环的 RE0_HOST，远程访问请用 SSH 隧道")
    print("            配额：" + "、".join(
        f"{label} {limits[name] if limits[name] else '不限'}"
        for name, label in (("RE0_REQUESTS_PER_MINUTE", "每账户请求/分钟"),
                            ("RE0_TASKS_PER_HOUR", "每账户任务/小时"),
                            ("RE0_SITE_REQUESTS_PER_MINUTE", "全站请求/分钟")))
        + ("（已显式设置；未设置的项仍不限。全站熔断始终生效）" if not deployment.hosted and any(
               os.getenv(name, "").strip() for name in limits) else
           "（本地模式默认不限，显式设置即生效；全站熔断始终生效）" if not deployment.hosted else
           "（未显式设置即为默认值；全站熔断始终生效，模型服务连续不可用时暂停新任务）"))
    return True


def doctor(probe_network: bool = False) -> int:
    """Report what can run now, without a model and without spending anything.

    A mode that could not start is reported as a failure rather than as one more line of output:
    doctor is what an operator checks before trusting the rest of the report.
    """
    print(f"Re0 doctor — re0-research {version()}")
    print(f"python: {sys.version.split()[0]} (this package needs >=3.11)")
    print(f"package: {Path(__file__).resolve().parent}")
    print()
    startable = print_deployment()
    print()
    print("mode: `paper search`, `paper text`, `mcp`, `skill`, `zotero`, `auth` and")
    print("      `session list/show/delta/scope` need no model key; a standalone task does,")
    print("      and so do `session follow-up`/`retry`")
    print("      (those two post to the running local service, where the key lives in memory)")
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
        return 0 if startable else 2
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
    return 0 if startable else 2


def skill_command(arguments) -> int:
    """Package, install or describe the skill directory. No network, no model, no spending."""
    from re0.skill_package import (SKILL_NAME, SkillPackageError, apply_install, locate_skill,
                                   manifest, package_into, plan_install, render_plan)
    try:
        if arguments.action == "show":
            root, origin = locate_skill()
            document = manifest(root, origin=origin)
            print(f"skill: {document['skill']}")
            print(f"directory: {root}")
            print(f"found via: {origin}")
            print(f"re0 version: {document['re0_version']}")
            print(f"tree sha256: {document['tree_sha256']}")
            for entry in document["files"]:
                print(f"  {entry['sha256'][:12]}  {entry['bytes']:>7}  {entry['path']}")
            return 0
        if arguments.action == "package":
            result = package_into(arguments.output)
            print(f"packaged from: {result['packaged_from']}")
            print(f"found via: {result['origin']}")
            print(f"wrote {len(result['written'])} files to {result['target']}")
            for name in result["written"]:
                print(f"  + {name}")
            print(f"manifest: {result['manifest']}")
            print("this is a copy of the skill, not an archive: install it with "
                  "`re0 skill install --target <host skills dir>`, or copy it by hand.")
            return 0
        if arguments.action == "install":
            # `--target` names the host's skills directory, not the skill's own directory: a host
            # keeps one directory per skill, and installing SKILL.md loose into the root of it
            # would be wrong in a way that is easy to miss afterwards.
            plan = plan_install(Path(arguments.target) / SKILL_NAME)
            if arguments.dry_run:
                print(render_plan(plan))
                print()
                print("dry run: nothing was written.")
                return 0
            if plan.conflicts and not arguments.force:
                print(render_plan(plan))
                print()
                print("refused: the conflicting files above were left untouched. Re-run with "
                      "--force to replace them (each old file is renamed aside, not deleted), "
                      "or move them yourself first.", file=sys.stderr)
                return 3
            applied = apply_install(plan, force=arguments.force)
            print(render_plan(plan, applied=applied))
            return 0
    except SkillPackageError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print("pass one of: show, package --output DIR, install --target DIR", file=sys.stderr)
    return 2


def build_parser() -> argparse.ArgumentParser:
    from re0.skill_package import SKILL_NAME  # local, so the CLI still imports without the package
    parser = argparse.ArgumentParser(prog=PROGRAM, description="Re0 research entry points.")
    parser.add_argument("--version", action="version", version=f"{PROGRAM} {version()}")
    subcommands = parser.add_subparsers(dest="command")

    paper = subcommands.add_parser("paper", help="literature retrieval (no model key needed)")
    paper_actions = paper.add_subparsers(dest="action")
    paper_actions.add_parser("search", help="search five scholarly sources and merge them")
    paper_actions.add_parser("text", help="read one open-access paper's full text, with locators")

    doctor_parser = subcommands.add_parser("doctor", help="report what can run without spending")
    doctor_parser.add_argument("--probe-network", action="store_true",
                               help="also run the network probes (spends requests; never implied)")

    subcommands.add_parser("mcp", help="serve the read-only tools over stdio (no model key needed)")

    # Its own parser, reached by passthrough: `re0 session follow-up --help` must print the real
    # flags rather than a thinner copy of them kept here.
    subcommands.add_parser("session", add_help=False,
                           help="inspect a research conversation, and continue one (no model key "
                                "needed to look; starting a turn goes through the local service)")
    subcommands.add_parser("zotero", add_help=False,
                           help="read-only incremental sync from one Zotero library; previews "
                                "before it writes, and never reads notes or attachments")
    subcommands.add_parser("auth", add_help=False,
                           help="hosted mode only: print a session secret, provision an account, "
                                "list, disable or end sessions (the password is never a flag)")

    skill = subcommands.add_parser("skill",
                                   help="package or install the skill directory (no model key needed)")
    skill_actions = skill.add_subparsers(dest="action")
    skill_actions.add_parser("show",
                             help="report which skill directory would be shipped, and its hashes")
    packager = skill_actions.add_parser(
        "package", help="write a self-contained copy with a hash manifest, for hand-delivery")
    packager.add_argument("--output", required=True, metavar="DIR",
                          help="directory to create <skill>/ inside. It must not already hold a "
                               "non-empty copy: nothing here overwrites.")
    installer = skill_actions.add_parser(
        "install", help="install into a host's skills directory, previewing first")
    installer.add_argument("--target", required=True, metavar="DIR",
                           help="the host skills directory to install into, e.g. ~/.claude/skills. "
                                f"The skill is created as DIR/{SKILL_NAME}, the way a host expects "
                                "one directory per skill.")
    installer.add_argument("--dry-run", action="store_true",
                           help="print the preview and write nothing")
    installer.add_argument("--force", action="store_true",
                           help="replace files that differ, renaming each old one aside as "
                                ".re0-backup-<timestamp> rather than deleting it. Without this a "
                                "conflicting file is left alone and reported.")
    return parser


def main(argv=None) -> int:
    """Dispatch, passing the search flags through untouched.

    The passthrough is split off *before* argparse runs, so `re0 paper search --help` reaches the
    skill's own parser and prints the real flags rather than a second, thinner copy of them.
    """
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv[:1] == ["session"]:
        # Dispatched before this parser runs, so the session subcommands keep their own flags and
        # their own help text instead of a second, thinner copy of them here.
        from re0.session_cli import main as session_main
        return session_main(argv[1:])
    if argv[:1] == ["zotero"]:
        from re0.zotero_cli import main as zotero_main
        return zotero_main(argv[1:])
    if argv[:1] == ["auth"]:
        from re0.auth_cli import main as auth_main
        return auth_main(argv[1:])
    passthrough: list = []
    if argv[:2] in (["paper", "search"], ["paper", "text"]):
        argv, passthrough = argv[:2], argv[2:]
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
    if arguments.command == "paper" and arguments.action == "text":
        from re0.fulltext_cli import main as text_main
        return text_main(passthrough)
    if arguments.command == "mcp":
        from re0.mcp_server import main as mcp_main
        mcp_main()
        return 0
    if arguments.command == "skill":
        return skill_command(arguments)
    parser.print_help()
    return 2
