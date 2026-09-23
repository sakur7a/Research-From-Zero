"""Where this copy of re0 finds its files — because a checkout and an installed wheel differ.

Everything else in the package assumes it can walk up from `re0/…` to a repository root. That is true
in a checkout and false in a wheel, where `re0/main.py` sits in `site-packages` and its great-grandparent
is the interpreter's own `lib/` directory. The two consequences were both real:

* `WEB = ROOT / "web"` pointed at `<prefix>/lib/web`, which does not exist, so `create_app` raised
  inside `StaticFiles(directory=…)` — an installed distribution had no user interface at all, and could
  not start.
* The default database path became `<prefix>/lib/.data/re0.sqlite3`, i.e. state written into the
  package directory, which is both wrong and (in a system install) unwritable.

So the two questions get one module, answered in a fixed order with the winner reported: an explicit
override, then an installed data directory, then a checkout. An installed copy is preferred over a
nearby checkout on purpose — a machine that installed the distribution should run what it installed,
not whatever source tree happens to sit next to it.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

# The files a wheel must carry alongside the Python modules. `web/` is the browser UI; `skills/` is
# shipped by the same mechanism (see `skill_package.py`), and `pyproject.toml` lists both by name
# because TOML cannot glob — the drift is pinned in `tests/test_paths.py`.
WEB_MARKER = "login.html"
SKILL_MARKER = "SKILL.md"


class LayoutError(RuntimeError):
    """A file this distribution needs is not where any known layout puts it."""


def _overrides(name: str) -> Path | None:
    raw = (os.environ.get(name) or "").strip()
    return Path(raw).expanduser() if raw else None


def checkout_root() -> Path | None:
    """The repository root, if this module is running from a checkout.

    Detected by content, not by depth: `web/login.html` exists only at a real root, so a stray
    `parents[2]` guess in an installed copy cannot pass for one.
    """
    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / "web" / WEB_MARKER).is_file() and (parent / "pyproject.toml").is_file():
            return parent
    return None


def installed_data_dir() -> Path | None:
    """`<prefix>/share/re0`, where setuptools' `data-files` put the shipped trees."""
    for prefix in {sys.prefix, getattr(sys, "base_prefix", sys.prefix)}:
        candidate = Path(prefix) / "share" / "re0"
        if (candidate / "web" / WEB_MARKER).is_file() or (candidate / "skills").is_dir():
            return candidate
    return None


def web_directory() -> Path:
    """The browser UI: an override, then the installed copy, then the checkout.

    Raises rather than returning a path that does not exist. A silent fallback here would produce a
    server that starts and 404s on every page, which reads to the next person like a broken route
    rather than a missing file.
    """
    override = _overrides("RE0_WEB_DIR")
    if override is not None:
        if (override / WEB_MARKER).is_file():
            return override
        raise LayoutError(f"RE0_WEB_DIR={override} 里没有 {WEB_MARKER}；这一目录应当是随分发包安装的 web 树")
    installed = installed_data_dir()
    if installed is not None:
        return installed / "web"
    root = checkout_root()
    if root is not None:
        return root / "web"
    raise LayoutError(
        "找不到浏览器界面文件。它们随分发包安装在 <prefix>/share/re0/web，或在仓库的 web/ 里；"
        "也可以用 RE0_WEB_DIR 显式指定。安装后出现这句话，通常说明这个 wheel 没有把 web/ 打进去。")


def default_database() -> Path:
    """The SQLite path to use when `RE0_DB` was not set.

    A checkout keeps the path it has always used (`.data/re0.sqlite3` at the repository root), because
    changing that for people with an existing library would lose their data in favour of an empty one.
    An installed copy writes under `~/.re0` instead of inside the package directory, which is the
    difference between state and a file that happens to sit next to the code — and `RE0_DATA_DIR`
    overrides both, for a deployment that wants state on another volume.
    """
    override = _overrides("RE0_DATA_DIR")
    if override is not None:
        return override / "re0.sqlite3"
    root = checkout_root()
    if root is not None:
        return root / ".data" / "re0.sqlite3"
    return Path.home() / ".re0" / "re0.sqlite3"


def describe() -> dict:
    """Where the pieces are, for `re0 doctor` — paths only, never contents."""
    installed, root = installed_data_dir(), checkout_root()
    out = {"layout": "checkout" if root else ("installed" if installed else "unknown")}
    if root:
        out["checkout"] = str(root)
    if installed:
        out["data-files"] = str(installed)
    try:
        out["web"] = str(web_directory())
    except LayoutError as exc:
        out["web"] = f"缺失：{exc}"
    out["database-default"] = str(default_database())
    return out
