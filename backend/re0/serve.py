"""Start the service, from a checkout or from an installed wheel.

`run.py` at the repository root used to hold this logic, which meant an installed distribution had no
way to start its own server: the script is not shipped, and it also assumed a `backend/` directory next
to it. Both live here now, so `python run.py` and `re0 serve` are the same code and one of them works
wherever the package is installed.

Two refusals are the point of the module:

* a non-loopback bind while the mode is `local`, because a process cannot see who can reach its port
  and an unauthenticated service on `0.0.0.0` is not a configuration but an open door;
* more than one worker, always — task state, the model vault, the execution lease and the rate-limit
  windows are per process, so a second worker would be a second copy of each, disagreeing.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}


def check_deployment(host: str) -> None:
    """Fail before anything is listening, with a message an operator can act on.

    Two different mistakes look alike from the outside — "I meant to serve this publicly" and "I
    typo'd RE0_HOST" — so both are named here rather than left to a traceback from inside uvicorn.
    The mode is declared rather than inferred: the process cannot see who can reach its port.
    """
    from re0.deployment import DeploymentError, from_env

    deployment = from_env()
    try:
        deployment.require_startable()
    except DeploymentError as exc:
        raise SystemExit(str(exc)) from exc
    if deployment.mode == "local" and host not in LOOPBACK_HOSTS:
        raise SystemExit(
            f"RE0_HOST={host!r} 不是回环地址，而 RE0_MODE 是 local（或未设置）。\n"
            "本地模式没有账户也没有登录，绑到非回环地址等于把文献库和任务接口开放给整个网络。\n"
            "- 只给自己用：把 RE0_HOST 设回 127.0.0.1（默认值），需要远程访问请用 SSH 隧道。\n"
            "- 确实要对外提供：设 RE0_MODE=hosted，并配好 RE0_SESSION_SECRET、RE0_PUBLIC_ENTRY、\n"
            "  RE0_ALLOWED_ORIGINS（用 `python -m re0 auth secret` 生成密钥，`python -m re0 auth\n"
            "  create-user` 开户）。缺任何一项，服务会拒绝启动并一次列全。")


def server_kwargs(environ=None) -> dict:
    """The uvicorn arguments, decided in one place so "one worker" is a fact with a test.

    Read from an environ mapping rather than the process directly, because the values worth checking
    are exactly the ones an operator sets in a unit file or a Docker `ENV`.
    """
    env = os.environ if environ is None else environ
    host = (env.get("RE0_HOST") or "127.0.0.1").strip()
    try:
        port = int((env.get("RE0_PORT") or "8000").strip())
    except ValueError as exc:
        raise SystemExit(f"RE0_PORT 必须是整数端口，收到 {env.get('RE0_PORT')!r}") from exc
    if not 1 <= port <= 65535:
        raise SystemExit(f"RE0_PORT={port} 不在可监听范围内（1–65535）")
    return {"host": host, "port": port, "workers": 1}


def load_env_file() -> None:
    """Honour `RE0_ENV_FILE` before anything reads a credential variable.

    Opt-in, names-only, and never a value printed: the same rules the skill follows, so the web UI and
    `re0 paper search` can share one file without either of them echoing what is in it.
    """
    from re0.env_file import load

    env_file = os.getenv("RE0_ENV_FILE", "").strip()
    if not env_file:
        return
    applied = load(env_file)
    print(f"loaded {len(applied)} variables from {env_file}" if applied
          else f"no usable variables in {env_file}")


def main(argv=None) -> int:
    """`python run.py` and `re0 serve` both land here."""
    load_env_file()
    kwargs = server_kwargs()
    check_deployment(kwargs["host"])
    try:
        from re0.paths import web_directory
        web_directory()
    except Exception as exc:
        # Checked before the port is bound: the failure mode it prevents is a server that starts,
        # answers /api/health, and 404s every page — which reads like a broken route to whoever has
        # to debug it, and as a working service to whoever finds it.
        print(f"浏览器界面文件不可用：{exc}", file=sys.stderr)
        return 2
    import uvicorn

    uvicorn.run("re0.main:app", **kwargs)
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised through run.py
    raise SystemExit(main())
