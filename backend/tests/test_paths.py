"""Does this distribution find its own files? A checkout and an installed wheel differ (#14 B1).

`main.py` used to compute the web directory as "two parents up from `re0/`", which is a repository
root in a checkout and the interpreter's own `lib/` in a wheel — so an installed copy could not start,
and would have written its database into the package directory if it had. These tests assert the
resolution order, the failure text, and one thing a unit test cannot fake: that an app pointed at a
directory shaped like an *installation* really serves every page.

The real build-and-install check is `scripts/install_smoke.py`, which needs `build` and a network for
build isolation; it reports what it actually ran. Nothing here claims a wheel was built.
"""
import re
import sys
import tomllib
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from re0 import paths
from re0.main import create_app

REPO_ROOT = Path(__file__).resolve().parents[2]
WEB = REPO_ROOT / "web"


def _refuse(request):
    raise AssertionError(f"Unexpected network request: {request.url}")


def installed_tree(prefix: Path, *, web: bool = True, skills: bool = False) -> Path:
    """A directory shaped like `<prefix>/share/re0/…`, the way setuptools' data-files land."""
    data = prefix / "share" / "re0"
    if web:
        target = data / "web"
        target.mkdir(parents=True, exist_ok=True)
        for name in ("login.html", "index.html", "agent.html", "search.html", "skill.html"):
            (target / name).write_text(f"<!doctype html><title>{name}</title>", encoding="utf-8")
        (target / "login-core.js").write_text("export const x = 1;", encoding="utf-8")
    if skills:
        (data / "skills" / "re0-paper-search").mkdir(parents=True, exist_ok=True)
    return data


# ---------------------------------------------------------------------------- the shipped file list

def test_the_declared_web_files_match_the_real_tree():
    """Drift guard: TOML cannot glob, so a page added to `web/` and not listed would simply be
    missing from every wheel built afterwards — and the installed service would 404 on it."""
    document = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    groups = (document["tool"]["setuptools"]["data-files"] or {})
    assert "share/re0/web" in groups, "the wheel declares no web directory at all"
    declared = {Path(name).name for name in groups["share/re0/web"]}
    on_disk = {path.name for path in WEB.rglob("*") if path.is_file()
               and "__pycache__" not in path.parts}
    assert declared == on_disk, (
        f"web/ and pyproject data-files disagree; only on disk: {sorted(on_disk - declared)}, "
        f"only declared: {sorted(declared - on_disk)}")


def test_the_web_tree_contains_the_pages_and_the_modules_they_import():
    served = {path.name for path in WEB.iterdir() if path.is_file()}
    for required in ("agent.html", "index.html", "login.html", "search.html", "skill.html",
                     "skill.css", "skill.js", "api.js", "core.js",
                     "login-core.js", "search-core.js", "agent-core.js", "theme-bootstrap.js",
                     "theme.css", "agent.css", "icons.js", "favicon.svg"):
        assert required in served, f"{required} is gone from web/"


def test_no_module_guesses_a_repository_layout_from_its_own_file():
    """The bug class, not the one instance of it.

    `Path(__file__).parents[n]` silently means something different in a wheel, and the n that works
    here depends on how deep `backend/` is mounted. Resource paths go through `re0.paths`, which
    looks for content and says so when it finds none.
    """
    offenders = []
    for module in (REPO_ROOT / "backend" / "re0").rglob("*.py"):
        if module.name == "paths.py" or "__pycache__" in module.parts:
            continue
        text = module.read_text(encoding="utf-8")
        if re.search(r"Path\(__file__\)[^\n]*\.parents\[\d+\]", text):
            offenders.append(module.name)
    assert not offenders, f"these files assume a checkout: {offenders}"


# -------------------------------------------------------------------------------- the resolution

def test_an_explicit_web_directory_wins_and_is_named_when_it_is_wrong(tmp_path, monkeypatch):
    good = installed_tree(tmp_path / "opt") / "web"
    monkeypatch.setenv("RE0_WEB_DIR", str(good))
    assert paths.web_directory() == good
    empty = tmp_path / "not-web"
    empty.mkdir()
    monkeypatch.setenv("RE0_WEB_DIR", str(empty))
    with pytest.raises(paths.LayoutError) as caught:
        paths.web_directory()
    assert "RE0_WEB_DIR" in str(caught.value) and "login.html" in str(caught.value)


def test_an_installed_copy_is_preferred_over_a_nearby_checkout(tmp_path, monkeypatch):
    """A machine that installed the distribution should run what it installed.

    The checkout is found by walking up from this file, so in a test run it always exists; only by
    preferring the installed probe can an install ever take effect.
    """
    monkeypatch.delenv("RE0_WEB_DIR", raising=False)
    assert paths.checkout_root() == REPO_ROOT, "the checkout probe itself"
    prefix = tmp_path / "venv"
    installed_tree(prefix, skills=True)
    monkeypatch.setattr(sys, "prefix", str(prefix))
    monkeypatch.setattr(sys, "base_prefix", str(prefix))
    assert paths.web_directory() == prefix / "share" / "re0" / "web"


def test_a_missing_web_tree_is_a_refusal_rather_than_a_server_with_404s(monkeypatch):
    monkeypatch.delenv("RE0_WEB_DIR", raising=False)
    monkeypatch.setattr(paths, "checkout_root", lambda: None)
    monkeypatch.setattr(paths, "installed_data_dir", lambda: None)
    with pytest.raises(paths.LayoutError) as caught:
        paths.web_directory()
    message = str(caught.value)
    assert "wheel" in message and "RE0_WEB_DIR" in message, message


# ---------------------------------------------------------------------------- the data location

def test_a_checkout_still_uses_the_path_it_always_used(tmp_path, monkeypatch):
    """Moving the default database would read as data loss to everybody who already has one."""
    monkeypatch.delenv("RE0_DATA_DIR", raising=False)
    assert paths.default_database() == REPO_ROOT / ".data" / "re0.sqlite3"


def test_an_install_writes_state_under_the_user_not_inside_the_package(tmp_path, monkeypatch):
    prefix = tmp_path / "site"
    installed_tree(prefix)
    monkeypatch.setattr(sys, "prefix", str(prefix))
    monkeypatch.setattr(sys, "base_prefix", str(prefix))
    monkeypatch.setattr(paths, "checkout_root", lambda: None)
    home = tmp_path / "home"
    monkeypatch.setattr(paths.Path, "home", lambda: home)
    database = paths.default_database()
    assert database == home / ".re0" / "re0.sqlite3", database
    assert "site" not in str(database), "state must not land inside an installed package tree"
    monkeypatch.setenv("RE0_DATA_DIR", str(tmp_path / "elsewhere"))
    assert paths.default_database().parent == tmp_path / "elsewhere"


def test_describe_names_the_layout_without_reading_any_contents(tmp_path, monkeypatch):
    facts = paths.describe()
    assert facts["layout"] == "checkout"
    assert facts["web"].endswith("web") and facts["database-default"].endswith("re0.sqlite3")
    assert "sqlite" in facts["database-default"]
    monkeypatch.setattr(paths, "checkout_root", lambda: None)
    monkeypatch.setattr(paths, "installed_data_dir", lambda: None)
    monkeypatch.delenv("RE0_WEB_DIR", raising=False)
    broken = paths.describe()
    assert broken["layout"] == "unknown" and broken["web"].startswith("缺失")


def test_doctor_prints_where_the_files_are(monkeypatch, capsys):
    """`re0 doctor` is where an operator looks when the service will not start."""
    from re0 import cli

    for var in ("RE0_MODE", "RE0_SESSION_SECRET", "RE0_PUBLIC_ENTRY", "RE0_ALLOWED_ORIGINS",
                "RE0_WEB_DIR", "RE0_DATA_DIR", "RE0_DB"):
        monkeypatch.delenv(var, raising=False)
    assert cli.main(["doctor"]) == 0
    out = capsys.readouterr().out
    assert "web 资源" in out and "数据库默认路径" in out and "布局 checkout" in out


# ------------------------------------------------------ an app served from an install-shaped directory

def test_the_service_serves_every_page_without_a_repository(tmp_path, monkeypatch):
    """The claim in one test: point the app at a directory shaped like an installation and the pages
    come out of it, so nothing in the request path still depends on a checkout.

    This is not a wheel build — the files are copies of the real ones, which is what an install would
    have placed there. `scripts/install_smoke.py` is the step that builds and installs for real.
    """
    target = tmp_path / "prefix"
    installed_tree(target)
    for name in ("agent.html", "index.html", "login.html", "search.html", "skill.html"):
        text = (WEB / name).read_text(encoding="utf-8")
        (target / "share" / "re0" / "web" / name).write_text(text, encoding="utf-8")
    for name in ("api.js", "core.js", "login-core.js", "login.js", "login.css", "theme.js",
                 "theme.css", "theme-bootstrap.js", "agent.css", "agent.js", "agent-core.js",
                 "app.js", "icons.js", "search.js", "search-core.js", "search.css", "skill.js",
                 "skill.css", "styles.css",
                 "favicon.svg"):
        (target / "share" / "re0" / "web" / name).write_bytes((WEB / name).read_bytes())
    monkeypatch.setenv("RE0_WEB_DIR", str(target / "share" / "re0" / "web"))
    monkeypatch.setenv("RE0_DATA_DIR", str(tmp_path / "state"))
    app = create_app(str(tmp_path / "installed.sqlite3"), httpx.MockTransport(_refuse))
    with TestClient(app) as client:
        for page in ("/", "/library", "/login", "/static/search.html", "/static/skill.html"):
            response = client.get(page)
            assert response.status_code == 200, f"{page}: {response.status_code}"
            assert "<html" in response.text.lower(), page
        for asset in ("/static/api.js", "/static/login-core.js", "/static/theme-bootstrap.js",
                      "/static/login.css", "/static/skill.css", "/static/skill.js",
                      "/static/favicon.svg"):
            assert client.get(asset).status_code == 200, asset
        assert client.get("/api/health").json()["status"] == "ok"
    # And an app built without an explicit path puts its state where RE0_DATA_DIR said, not beside
    # the installed tree: the two failure modes here were "no UI" and "data inside site-packages".
    second = create_app(transport=httpx.MockTransport(_refuse))
    assert second.state.store.db.path.startswith(str(tmp_path / "state")), second.state.store.db.path
    assert (tmp_path / "state").is_dir()

# --------------------------------------------------------------------------- starting an installed copy

def test_the_two_entry_points_are_one_implementation(tmp_path):
    """`python run.py` and `re0 serve` must not be two copies of the startup rules.

    The wheel ships neither `run.py` nor a `backend/` next to it, so an installed deployment can only
    reach the server through the console script — and a guard that exists in one of the two is a guard
    that can be walked around by choosing the other.
    """
    import importlib.util

    from re0 import serve

    spec = importlib.util.spec_from_file_location("re0_run_script", REPO_ROOT / "run.py")
    launcher = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(launcher)
    assert launcher.check_deployment is serve.check_deployment
    assert launcher.LOOPBACK_HOSTS is serve.LOOPBACK_HOSTS
    assert launcher.main is serve.main, "the two entry points diverged"
    pyproject = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert "re0 = \"re0.cli:main\"" in pyproject,         "the console script is how an installed distribution reaches `re0 serve`"


def test_one_worker_is_a_fact_rather_than_a_default(monkeypatch):
    from re0 import serve

    assert serve.server_kwargs({}) == {"host": "127.0.0.1", "port": 8000, "workers": 1}
    # A public bind is not decided here — `check_deployment` refuses it in local mode — but the worker
    # count is, and it is not overridable by an environment variable.
    assert serve.server_kwargs({"RE0_HOST": "0.0.0.0", "RE0_PORT": "9000",
                                "RE0_WORKERS": "4"})["workers"] == 1
    assert serve.server_kwargs({"RE0_HOST": "0.0.0.0"})["host"] == "0.0.0.0"
    assert serve.server_kwargs({"PORT": "10000", "RE0_PORT": "8000"})["port"] == 10000
    assert serve.server_kwargs({"RE0_PORT": "9000"})["port"] == 9000


def test_a_nonsense_port_is_refused_before_uvicorn_is_called():
    from re0 import serve

    for name in ("PORT", "RE0_PORT"):
        for bad in ("http://8000", "8000-9000", "yes"):
            with pytest.raises(SystemExit) as caught:
                serve.server_kwargs({name: bad})
            assert name in str(caught.value)
    with pytest.raises(SystemExit) as out_of_range:
        serve.server_kwargs({"RE0_PORT": "0"})
    assert "1–65535" in str(out_of_range.value)
    with pytest.raises(SystemExit) as injected_out_of_range:
        serve.server_kwargs({"PORT": "65536", "RE0_PORT": "8000"})
    assert "PORT=65536" in str(injected_out_of_range.value)


def test_startup_refuses_when_the_interface_files_are_missing(monkeypatch, tmp_path):
    """A server that answers /api/health and 404s every page is worse than one that will not start.

    That shape is what an install with a `web/` tree missing from the wheel used to produce, and it
    reads as a broken route to the next person and as a working service to the one who finds it.
    """
    import types

    from re0 import serve

    called = []
    monkeypatch.setitem(sys.modules, "uvicorn", types.SimpleNamespace(
        run=lambda *args, **kwargs: called.append(kwargs)))
    monkeypatch.setenv("RE0_HOST", "127.0.0.1")
    monkeypatch.delenv("RE0_PORT", raising=False)
    monkeypatch.delenv("PORT", raising=False)
    monkeypatch.delenv("RE0_MODE", raising=False)
    monkeypatch.delenv("RE0_SESSION_SECRET", raising=False)
    monkeypatch.delenv("RE0_ENV_FILE", raising=False)
    assert serve.main([]) == 0 and called, "the healthy path does reach uvicorn"
    assert called[0]["workers"] == 1

    called.clear()

    def missing():
        raise paths.LayoutError("找不到浏览器界面文件")

    # `serve.main` imports the resolver at call time, so patching the module it lives in is the seam.
    monkeypatch.setattr(paths, "web_directory", missing)
    assert serve.main([]) == 2 and not called, "uvicorn was started despite the missing UI"
