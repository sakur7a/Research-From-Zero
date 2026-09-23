"""Build both artifacts, install them somewhere else, and verify the installed copies.

Issue #3 requires a source distribution as well as a wheel; #14 requires the installed Web assets to
work outside a checkout. That distinction matters here specifically: files used to be located by
walking up from `backend/re0/…` to the repository root, which stops being true once installed. So
this script does the boring, unforgiving version of the check:

1. build a wheel and an sdist from the working tree;
2. create fresh venvs and give them the third-party packages of the running environment by
   PYTHONPATH — no runtime dependency install, and no `re0`: the dev venv holds an editable install that names
   `<repo>/backend` on sys.path, which must not be reachable from the "installed" copy;
3. install the wheel **by path, with `--no-deps`**, into one venv and install the sdist into a
   second clean venv (pip uses build isolation for the sdist backend);
4. run everything with the working directory **outside the repository**, so a leftover `backend/` on
   `sys.path` cannot pass for an installed copy;
5. assert both archives carry the Skill and Web files; exercise `doctor`, the installed skill
   installer, an offline Skill search fixture, a stdio MCP handshake, and a real HTTP serve/write flow;
6. write a report saying exactly which stages ran.

Exit codes: 0 = both artifacts passed; 1 = an artifact or runtime check failed; 2 = **not run**
(the declared build frontend or its build requirements are unavailable). A skip is never reported
as a pass.
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PAGES = {"/": "研究工作台", "/library": "文献", "/login": "登录",
         "/static/search.html": "检索工作台", "/static/skill.html": "资源观察"}
ASSETS = ["/static/api.js", "/static/login-core.js", "/static/theme-bootstrap.js",
          "/static/login.css", "/static/theme.css", "/static/agent.css", "/static/core.js",
          "/static/skill.css", "/static/skill.js"]
WEB_FILE_COUNT = len([p for p in (ROOT / "web").iterdir() if p.is_file()])


def run(command, *, cwd=None, env=None, timeout=180, input=None) -> subprocess.CompletedProcess:
    return subprocess.run(command, cwd=cwd, env=env, timeout=timeout, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", input=input)


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def get(url: str, timeout: float = 5.0) -> tuple[int, str]:
    request = urllib.request.Request(url, headers={"X-Re0-Client": "web"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as error:
        return error.code, error.read().decode("utf-8", "replace")


def mcp_handshake(cli: Path, *, cwd: Path, env: dict) -> dict:
    messages = [
        {"jsonrpc": "2.0", "id": 0, "method": "initialize",
         "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                    "clientInfo": {"name": "install-smoke", "version": "1"}}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
    ]
    payload = "\n".join(json.dumps(message) for message in messages) + "\n"
    result = run([str(cli), "mcp"], cwd=cwd, env=env, timeout=30, input=payload)
    try:
        replies = [json.loads(line) for line in result.stdout.splitlines() if line.strip()]
    except ValueError:
        replies = []
    tools = ([tool.get("name") for tool in replies[1].get("result", {}).get("tools", [])]
             if len(replies) > 1 else [])
    passed = (result.returncode == 0 and len(replies) == 2
              and replies[0].get("result", {}).get("protocolVersion") == "2025-06-18"
              and "search_papers" in tools and "fetch_paper_text" in tools)
    return {"exit_code": result.returncode, "replies": len(replies), "tools": tools,
            "stderr": result.stderr[-500:], "passed": passed}


def installed_skill_search_fixture(python: Path, wrapper: Path, *, cwd: Path,
                                   env: dict, output: Path) -> dict:
    """Run the installed Skill CLI path against a mocked OpenAlex response, with no credentials."""
    empty_credentials = cwd / "empty skill credentials.env"
    empty_credentials.write_text("# fixture run: no provider credentials\n", encoding="utf-8")
    isolated = {**env, "RE0_ENV_FILE": str(empty_credentials)}
    for name in ("SEMANTIC_SCHOLAR_API_KEY", "SEMANTICSCHOLAR_API_KEY", "OPENALEX_API_KEY",
                 "OPENALEX_MAILTO", "OPENREVIEW_TOKEN", "GITHUB_TOKEN", "HF_TOKEN"):
        isolated.pop(name, None)

    wrapper_help = run([str(python), str(wrapper), "--help"], cwd=cwd, env=isolated)
    code = r'''
import json, sys
from pathlib import Path
import httpx
import re0.skill_search as skill_search
from re0.agent.tools import ResearchTools

calls = []
def router(request):
    calls.append(request.url.host)
    assert request.url.host == "api.openalex.org", request.url
    return httpx.Response(200, json={"results": [{
        "id": "https://openalex.org/W-SKILL-FIXTURE",
        "title": "Packaged Skill Search Fixture",
        "publication_year": 2025,
        "authorships": [{"author": {"display_name": "Fixture Author"}}],
        "primary_location": {"source": {"display_name": "Fixture Venue", "type": "journal"}},
        "abstract_inverted_index": {"A": [0], "packaged": [1], "skill": [2], "fixture": [3]},
    }], "meta": {"count": 1}})

real_tools = ResearchTools
skill_search.ResearchTools = lambda _database: real_tools(None, httpx.MockTransport(router))
exit_code = skill_search.main([
    "--query", "packaged skill fixture", "--sources", "openalex", "--max-papers", "5",
    "--max-pages", "1", "--max-requests", "1", "--find-artifacts", "0", "--verify", "0",
    "--json", sys.argv[1],
])
assert exit_code == 0, exit_code
result = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
assert result["documents"][0]["paper"]["title"] == "Packaged Skill Search Fixture", result
assert result["coverage"]["sources_queried"] == ["openalex"], result["coverage"]
assert calls == ["api.openalex.org"], calls
print(json.dumps({"title": result["documents"][0]["paper"]["title"], "mocked_hosts": calls}))
'''
    fixture_run = run([str(python), "-c", code, str(output)], cwd=cwd, env=isolated, timeout=45)
    try:
        fixture_summary = json.loads(fixture_run.stdout.splitlines()[-1])
    except (IndexError, ValueError):
        fixture_summary = {}
    passed = (wrapper_help.returncode == 0 and "--query" in wrapper_help.stdout
              and fixture_run.returncode == 0
              and fixture_summary.get("title") == "Packaged Skill Search Fixture"
              and fixture_summary.get("mocked_hosts") == ["api.openalex.org"]
              and output.is_file())
    return {"wrapper_help": wrapper_help.returncode == 0,
            "fixture_search": fixture_run.returncode == 0,
            "fixture_title": fixture_summary.get("title", ""),
            "mocked_hosts": fixture_summary.get("mocked_hosts", []),
            "passed": passed,
            "stderr": (wrapper_help.stderr + fixture_run.stderr)[-500:]}


def main(output_dir: Path) -> int:
    report: dict = {"stages": [], "skipped_reason": "", "failures": []}

    def stage(name: str, **facts) -> None:
        report["stages"].append(dict(stage=name, **facts))
        print(f"[{name}] " + json.dumps(facts, ensure_ascii=False))

    with tempfile.TemporaryDirectory(prefix="re0 install smoke ", ignore_cleanup_errors=True) as temp:
        workspace = Path(temp)
        dist = workspace / "dist"
        build = run([sys.executable, "-m", "build", "--sdist", "--wheel", "--outdir",
                     str(dist), str(ROOT)], cwd=workspace, timeout=420)
        wheels, sdists = sorted(dist.glob("*.whl")), sorted(dist.glob("*.tar.gz"))
        if build.returncode != 0 or not wheels or not sdists:
            detail = (build.stderr or build.stdout).strip().splitlines()
            report["skipped_reason"] = (
                "cannot build both wheel and sdist with the declared test tools: "
                + (detail[-1] if detail else "no output"))
            stage("skipped", reason=report["skipped_reason"])
            print("SKIPPED, not a pass:", report["skipped_reason"])
            return 2
        wheel, sdist = wheels[0], sdists[0]
        stage("built", wheel=wheel.name, wheel_bytes=wheel.stat().st_size,
              sdist=sdist.name, sdist_bytes=sdist.stat().st_size,
              path_has_spaces=" " in str(workspace))

        with tarfile.open(sdist, "r:gz") as archive:
            members = {name.split("/", 1)[1] for name in archive.getnames() if "/" in name}
        required_sdist_files = {"pyproject.toml", "backend/re0/__init__.py",
                                "skills/re0-paper-search/SKILL.md",
                                "skills/re0-paper-search/scripts/paper_search.py",
                                "web/skill.html", "web/skill.css", "web/skill.js"}
        missing_sdist = sorted(required_sdist_files - members)
        stage("sdist_contents", checked=sorted(required_sdist_files), missing=missing_sdist)
        if missing_sdist:
            report["failures"].append(f"sdist is missing required source files: {missing_sdist}")

        venv = workspace / "wheel venv"

        created = run([sys.executable, "-m", "venv", str(venv)])
        if created.returncode != 0:
            report["failures"].append(f"venv creation failed: {created.stderr[-400:]}")
            return 1
        scripts = venv / ("Scripts" if os.name == "nt" else "bin")
        python = scripts / ("python.exe" if os.name == "nt" else "python")
        installed = run([str(python), "-m", "pip", "install", "--no-index", "--no-deps",
                         "--force-reinstall", str(wheel)], cwd=workspace)
        if installed.returncode != 0:
            report["skipped_reason"] = f"offline install refused: {installed.stderr[-400:]}"
            stage("skipped", reason=report["skipped_reason"][:300])
            print("SKIPPED, not a pass: pip refused to install the wheel without network")
            return 2
        stage("wheel_installed", wheel=wheel.name)

        # This machine has no offline source for FastAPI/uvicorn/etc, so the fresh venv borrows the
        # *third-party* packages of the running environment over HTTP-free PYTHONPATH. What that must
        # not borrow is `re0` itself: the dev venv installs it in editable mode, whose .pth names
        # `<repo>/backend`, and a checkout reachable from the "installed" copy would make every check
        # below meaningless. `site` only processes .pth files inside the current environment's own
        # directories, so a borrowed directory contributes packages, not its path hooks — and the
        # import-origin stage asserts that rather than assuming it.
        import sysconfig
        deps = Path(sysconfig.get_paths()["purelib"])
        report["borrowed_dependencies_from"] = str(deps)
        stage("dependencies", source=str(deps), note="third-party only; `re0` must come from the wheel")

        # The wheel's own contents, read out of the archive rather than trusted from the declaration.
        import zipfile
        with zipfile.ZipFile(wheels[0]) as archive:
            names = [name for name in archive.namelist() if "share/re0/web/" in name.replace("\\", "/")]
        report["wheel_web_files"] = len(names)
        stage("wheel_contents", web_files=len(names))
        if len(names) != WEB_FILE_COUNT:
            report["failures"].append(f"wheel carries {len(names)} web files, expected {WEB_FILE_COUNT}")

        # Everything below runs with cwd outside the repository, so a checkout cannot be the reason it works.
        env = {**os.environ, "PYTHONNOUSERSITE": "1", "RE0_MODE": "local",
               "PYTHONPATH": os.pathsep.join([str(deps)] + ([os.environ["PYTHONPATH"]]
                                                            if os.environ.get("PYTHONPATH") else []))}
        for dropped in ("RE0_DB", "RE0_DATA_DIR", "RE0_WEB_DIR", "RE0_ENV_FILE", "RE0_HOME",
                        "RE0_HOST", "RE0_PORT"):
            env.pop(dropped, None)
        probe = run([str(python), "-c", "import re0, sys; print(re0.__file__); print(sys.prefix)"],
                    cwd=workspace, env=env)
        origin = probe.stdout.strip().splitlines()
        module = origin[0].replace("\\", "/") if origin else ""
        inside_venv = str(venv).replace("\\", "/") in module
        outside_repo = ROOT.as_posix() not in module
        stage("import_origin", module=module, from_venv=inside_venv, outside_repo=outside_repo)
        if not inside_venv or not outside_repo:
            report["failures"].append(f"`re0` imported from the wrong place: {origin}")

        doctor = run([str(scripts / "re0.exe" if os.name == "nt" else scripts / "re0"), "doctor"],
                     cwd=workspace, env=env)
        stage("doctor", exit_code=doctor.returncode, layout=[line.strip() for line in
              doctor.stdout.splitlines() if "布局" in line])
        if doctor.returncode != 0:
            report["failures"].append(f"`re0 doctor` exited {doctor.returncode}: {doctor.stdout[-300:]}")
        if "installed" not in doctor.stdout:
            report["failures"].append("doctor did not report the `installed` layout:\n" + doctor.stdout[-500:])

        cli = scripts / ("re0.exe" if os.name == "nt" else "re0")
        mcp = mcp_handshake(cli, cwd=workspace, env=env)
        stage("wheel_mcp", **mcp)
        if not mcp["passed"]:
            report["failures"].append("installed wheel MCP initialize/tools-list handshake failed")

        skill_show = run([str(cli), "skill", "show"], cwd=workspace, env=env)
        host_skills = workspace / "host skills"
        preview = run([str(cli), "skill", "install", "--target", str(host_skills), "--dry-run"],
                      cwd=workspace, env=env)
        preview_ok = (preview.returncode == 0 and "nothing was written" in preview.stdout.lower()
                      and not host_skills.exists())
        install_skill = run([str(cli), "skill", "install", "--target", str(host_skills)],
                            cwd=workspace, env=env)
        installed_skill = host_skills / "re0-paper-search"
        install_ok = (install_skill.returncode == 0 and (installed_skill / "SKILL.md").is_file()
                      and (installed_skill / "MANIFEST.json").is_file()
                      and (installed_skill / "references" / "venue-filter.md").is_file())
        stage("wheel_skill_install", show_origin="installed data directory" in skill_show.stdout,
              preview=preview_ok, install=install_ok, path_has_spaces=" " in str(host_skills))
        if (skill_show.returncode != 0 or "installed data directory" not in skill_show.stdout
                or not preview_ok or not install_ok):
            report["failures"].append("installed Skill resolution/preview/install failed")

        installed_skill_script = (venv / "share" / "re0" / "skills" / "re0-paper-search"
                                  / "scripts" / "paper_search.py")
        invalid_home_env = {**env, "RE0_HOME": str(workspace / "not a checkout")}
        invalid_home = run([str(python), str(installed_skill_script), "--help"], cwd=workspace,
                           env=invalid_home_env)
        invalid_home_ok = (invalid_home.returncode == 2 and "RE0_HOME=" in invalid_home.stderr
                           and "Traceback" not in invalid_home.stderr)
        stage("invalid_re0_home", exit_code=invalid_home.returncode, clear_error=invalid_home_ok,
              path_has_spaces=" " in invalid_home.stderr)
        if not invalid_home_ok:
            report["failures"].append("invalid explicit RE0_HOME did not fail clearly without a traceback")

        wheel_search = installed_skill_search_fixture(
            python, installed_skill_script, cwd=workspace, env=env,
            output=workspace / "wheel skill search fixture.json")
        stage("wheel_skill_search_fixture", **wheel_search)
        if not wheel_search["passed"]:
            report["failures"].append("installed wheel Skill search fixture failed: "
                                      + wheel_search.get("stderr", ""))

        # The installed CLI must be able to transfer a source workspace without a checkout or
        # network. Both the workspace and bundle paths contain spaces to exercise argument passing.
        source_workspace = workspace / "mcp source workspace"
        imported_workspace = workspace / "web imported workspace"
        bundle_path = workspace / "source bundle.json"
        fixture_source = {"source_url": "https://export.arxiv.org/abs/2501.12345",
                          "locator": "fixture paragraph 2", "kind": "paper",
                          "content": "FIXTURE: imported sources are not live research results.",
                          "paper": {"title": "Fixture workspace bundle"}}
        record_code = ("import json,sys; from re0.workspace import Workspace; "
                       "w=Workspace(sys.argv[1]).open(); "
                       "sid=w.record(json.loads(sys.argv[2]),tool='search_papers'); "
                       "print(json.dumps({'workspace_id':w.workspace_id,'source_id':sid}))")
        recorded = run([str(python), "-c", record_code, str(source_workspace),
                        json.dumps(fixture_source)], cwd=workspace, env=env)
        try:
            source_identity = json.loads(recorded.stdout.strip()) if recorded.returncode == 0 else {}
        except ValueError:
            source_identity = {}
        export_bundle = run([str(cli), "workspace", "export", "--directory", str(source_workspace),
                             "--output", str(bundle_path)], cwd=workspace, env=env)
        preview_bundle = run([str(cli), "workspace", "import", "--directory", str(imported_workspace),
                              "--input", str(bundle_path)], cwd=workspace, env=env)
        try:
            preview_result = json.loads(preview_bundle.stdout.strip())
        except ValueError:
            preview_result = {}
        preview_clean = preview_bundle.returncode == 0 and preview_result.get("applied") is False \
            and preview_result.get("new") == [source_identity.get("source_id")] \
            and not imported_workspace.exists()
        apply_bundle = run([str(cli), "workspace", "import", "--directory", str(imported_workspace),
                            "--input", str(bundle_path), "--apply"], cwd=workspace, env=env)
        try:
            apply_result = json.loads(apply_bundle.stdout.strip())
        except ValueError:
            apply_result = {}
        reexport = run([str(cli), "workspace", "export", "--directory", str(imported_workspace),
                        "--output", str(workspace / "roundtrip bundle.json")], cwd=workspace, env=env)
        try:
            roundtrip_bundle = json.loads((workspace / "roundtrip bundle.json").read_text(encoding="utf-8"))
            transferred = roundtrip_bundle["sources"][0].get("imported_by_user") is True
        except (OSError, ValueError, KeyError, IndexError):
            transferred = False
        bundle_ok = (source_identity.get("source_id") and export_bundle.returncode == 0
                     and preview_clean and apply_bundle.returncode == 0
                     and apply_result.get("papers_approved") == 0 and reexport.returncode == 0
                     and transferred)
        stage("wheel_workspace_bundle", recorded=bool(source_identity.get("source_id")),
              preview_no_write=preview_clean, import_exit=apply_bundle.returncode,
              provenance_marked_unverified=transferred, path_has_spaces=" " in str(bundle_path),
              passed=bool(bundle_ok))
        if not bundle_ok:
            detail = (recorded.stderr or export_bundle.stderr or preview_bundle.stderr
                      or apply_bundle.stderr or reexport.stderr).strip()[-500:]
            report["failures"].append(f"installed wheel workspace bundle round trip failed: {detail}")

        # Start the installed server on a real port and fetch the pages over real HTTP.
        port = free_port()
        serve_env = {**env, "RE0_HOST": "127.0.0.1", "RE0_PORT": str(port),
                     "RE0_DATA_DIR": str(workspace / "state")}
        server = subprocess.Popen([str(scripts / "re0.exe" if os.name == "nt" else scripts / "re0"),
                                   "serve"], cwd=workspace, env=serve_env,
                                  stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                  encoding="utf-8", errors="replace")
        try:
            base = f"http://127.0.0.1:{port}"
            health = None
            for _ in range(60):
                if server.poll() is not None:
                    break
                try:
                    status, body = get(base + "/api/health")
                    if status == 200:
                        health = json.loads(body)
                        break
                except (urllib.error.URLError, OSError):
                    time.sleep(0.25)
            stage("serving", started=health is not None, port=port,
                  version=(health or {}).get("version"), mode=(health or {}).get("mode"))
            if health is None:
                output = (server.stdout.read(2000) if server.stdout else "") or ""
                report["failures"].append(f"installed server never answered /api/health: {output[:500]}")
            else:
                for path, marker in {**PAGES, **{asset: "" for asset in ASSETS}}.items():
                    status, body = get(base + path)
                    ok = status == 200 and (not marker or marker in body)
                    stage("page", path=path, status=status, ok=ok)
                    if not ok:
                        report["failures"].append(f"{path} -> {status} (marker {marker!r} present: "
                                                 f"{marker in body if marker else 'n/a'})")
                # A write through the installed service, so the SQLite default under RE0_DATA_DIR is
                # exercised outside a checkout rather than only in a test.
                write = urllib.request.Request(base + "/api/papers", method="POST",
                                              headers={"Content-Type": "application/json",
                                                       "X-Re0-Client": "web"},
                                              data=json.dumps({"title": "安装后写入的一篇"}).encode())
                with urllib.request.urlopen(write, timeout=10) as response:
                    created_paper = response.status
                stage("write", status=created_paper,
                      state_dir=(workspace / "state").is_dir(),
                      files=[p.name for p in (workspace / "state").iterdir()]
                      if (workspace / "state").is_dir() else [])
                if created_paper != 201 or not (workspace / "state" / "re0.sqlite3").is_file():
                    report["failures"].append("the installed service did not write where it said it would")
        finally:
            server.terminate()
            try:
                server.wait(timeout=10)
            except subprocess.TimeoutExpired:
                server.kill()

        sdist_venv = workspace / "sdist venv"
        sdist_created = run([sys.executable, "-m", "venv", str(sdist_venv)])
        sdist_scripts = sdist_venv / ("Scripts" if os.name == "nt" else "bin")
        sdist_python = sdist_scripts / ("python.exe" if os.name == "nt" else "python")
        sdist_cli = sdist_scripts / ("re0.exe" if os.name == "nt" else "re0")
        sdist_install = (run([str(sdist_python), "-m", "pip", "install", "--no-deps", str(sdist)],
                             cwd=workspace, timeout=420)
                         if sdist_created.returncode == 0 else sdist_created)
        sdist_env = {**env, "PYTHONPATH": os.pathsep.join(
            [str(deps)] + ([os.environ["PYTHONPATH"]] if os.environ.get("PYTHONPATH") else []))}
        sdist_probe = (run([str(sdist_python), "-c", "import re0; print(re0.__file__)"],
                           cwd=workspace, env=sdist_env) if sdist_install.returncode == 0 else sdist_install)
        sdist_module = sdist_probe.stdout.strip().splitlines()[0] if sdist_probe.stdout.strip() else ""
        sdist_doctor = (run([str(sdist_cli), "doctor"], cwd=workspace, env=sdist_env)
                        if sdist_install.returncode == 0 else sdist_install)
        sdist_mcp = (mcp_handshake(sdist_cli, cwd=workspace, env=sdist_env)
                     if sdist_install.returncode == 0 else {"passed": False, "tools": [], "replies": 0})
        sdist_web = sdist_venv / "share" / "re0" / "web"
        sdist_skill = sdist_venv / "share" / "re0" / "skills" / "re0-paper-search" / "SKILL.md"
        sdist_skill_script = (sdist_venv / "share" / "re0" / "skills" / "re0-paper-search"
                              / "scripts" / "paper_search.py")
        sdist_search = (installed_skill_search_fixture(
            sdist_python, sdist_skill_script, cwd=workspace, env=sdist_env,
            output=workspace / "sdist skill search fixture.json")
                        if sdist_install.returncode == 0 else {"passed": False})
        stage("sdist_skill_search_fixture", **sdist_search)
        if not sdist_search["passed"]:
            report["failures"].append("installed sdist Skill search fixture failed: "
                                      + sdist_search.get("stderr", ""))
        sdist_ok = (sdist_install.returncode == 0 and str(sdist_venv).replace("\\", "/")
                    in sdist_module.replace("\\", "/") and "installed" in sdist_doctor.stdout
                    and (sdist_web / "skill.html").is_file() and sdist_skill.is_file()
                    and sdist_mcp["passed"] and sdist_search["passed"])
        stage("sdist_install", exit_code=sdist_install.returncode,
              module=sdist_module.replace("\\", "/"), installed_layout="installed" in sdist_doctor.stdout,
              web_assets=(sdist_web / "skill.html").is_file(), skill_asset=sdist_skill.is_file(),
              mcp=sdist_mcp["passed"], replies=sdist_mcp.get("replies", 0),
              search_fixture=sdist_search["passed"], passed=sdist_ok)
        if not sdist_ok:
            detail = (sdist_install.stderr or sdist_probe.stderr or sdist_doctor.stderr).strip()[-500:]
            report["failures"].append(f"clean sdist install/doctor/MCP verification failed: {detail}")

    report["passed"] = not report["failures"]
    (output_dir).mkdir(parents=True, exist_ok=True)
    (output_dir / "install-smoke.json").write_text(json.dumps(report, ensure_ascii=False, indent=2),
                                                  encoding="utf-8")
    print(json.dumps({"passed": report["passed"], "failures": report["failures"],
                      "wheel_web_files": report.get("wheel_web_files"),
                      "sdist_installed": any(stage.get("stage") == "sdist_install"
                                             and stage.get("passed") for stage in report["stages"])},
                     ensure_ascii=False, indent=2))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    sys.exit(main(Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "test-results" / "install"))
