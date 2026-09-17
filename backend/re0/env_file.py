"""Opt-in dotenv loader.

Re0 does not read a `.env` at import time, and nothing here searches the filesystem
on its own: a caller passes an explicit path (the skill script, or `run.py` when
`RE0_ENV_FILE` is set). A stray file in a working directory therefore cannot silently
change which credentials or destinations are in use.

Keys are never written to disk, logs, task records or exports; the loader only fills
`os.environ` for the process, and a variable that is already set always wins so a real
environment cannot be shadowed by a file.
"""
from __future__ import annotations

import os
from pathlib import Path

MAX_BYTES = 64 * 1024
NAME_CHARS = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_")


def parse(text: str) -> dict[str, str]:
    """`KEY=VALUE` lines with `#` comments, an optional `export ` prefix and optional
    matching quotes. No interpolation and no command substitution: a value is data."""
    values: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        name, _, value = line.partition("=")
        name = name.strip()
        if not name or any(character not in NAME_CHARS for character in name):
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        values[name] = value
    return values


def load(path, *, override: bool = False) -> list[str]:
    """Set variables from `path` and return the names that were set.

    A missing, oversized or unreadable file is not an error: it returns `[]`, so an
    absent credential file degrades to "no key configured" rather than a crash.
    """
    if path in (None, ""):
        return []
    file = Path(path).expanduser()
    try:
        if not file.is_file() or file.stat().st_size > MAX_BYTES:
            return []
        text = file.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeDecodeError):
        return []
    applied = []
    for name, value in parse(text).items():
        if not value:
            continue  # an empty entry means "not configured", not "set to empty"
        if not override and os.environ.get(name):
            continue
        os.environ[name] = value
        applied.append(name)
    return sorted(applied)
