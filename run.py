"""Run from the repository root: python run.py.

`RE0_ENV_FILE` is honoured here so the web UI and the retrieval skill can share one
credential file. It is opt-in: with the variable unset nothing is read, and a variable
that is already set in the environment always wins over the file.
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "backend"))

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


if __name__ == "__main__":
    from re0.env_file import load

    env_file = os.getenv("RE0_ENV_FILE", "").strip()
    if env_file:
        applied = load(env_file)
        # Names only: a value must never reach a terminal, log or task record.
        print(f"loaded {len(applied)} variables from {env_file}" if applied
              else f"no usable variables in {env_file}")

    import uvicorn

    host = os.getenv("RE0_HOST", "127.0.0.1")
    check_deployment(host)
    # One worker, always: task state, the model vault and the execution lease are per process, so a
    # second worker would be a second, disagreeing copy of each.
    uvicorn.run("re0.main:app", host=host, port=int(os.getenv("RE0_PORT", "8000")), workers=1)
