"""`re0 auth` — provision accounts and session secrets from the server console.

There is no registration endpoint. An account on a service that holds other people's model keys and
library notes is created by whoever operates the server, from a shell on that server, and the password
never appears in a command line: argv is readable in the process list and lands in shell history, so
the password comes from a terminal prompt, from stdin, or from `RE0_AUTH_PASSWORD` in the environment.

In local mode none of this is needed — there is one implicit owner and nothing to log into — and the
commands say so rather than creating an account that would only confuse the two modes.
"""
from __future__ import annotations

import argparse
import getpass
import os
import sys
from pathlib import Path

from .auth import AccountStore, AuthError, new_secret
from .db import Database
from .deployment import LOCAL_OWNER, DeploymentError, from_env

PROGRAM = "re0 auth"
ROOT = Path(__file__).resolve().parents[2]
KEY_ENVIRON = "RE0_AUTH_PASSWORD"


def database_path(explicit: str = "") -> str:
    return explicit or os.getenv("RE0_DB") or str(ROOT / ".data" / "re0.sqlite3")


def _store(path: str) -> AccountStore:
    if not Path(path).is_file():
        raise AuthError(f"没有文献库数据库：{path}；先启动一次本地服务，或用 --db 指向已有的那一个")
    return AccountStore(Database(path))


def _read_password(arguments) -> str:
    """A password from somewhere that is not argv, in the order a person would reach for them."""
    if getattr(arguments, "password_stdin", False):
        return sys.stdin.readline().rstrip("\r\n")
    from_environ = os.getenv(KEY_ENVIRON, "")
    if from_environ:
        return from_environ
    if sys.stdin.isatty():
        first = getpass.getpass("口令（至少 10 个字符，不会回显）: ")
        again = getpass.getpass("再输入一次: ")
        if first != again:
            raise AuthError("两次输入不一致；没有创建任何账户")
        return first
    return sys.stdin.readline().rstrip("\r\n")


def main(argv=None) -> int:
    parser = build_parser()
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        parser.print_help()
        return 2
    try:
        arguments = parser.parse_args(argv)
    except SystemExit as exc:
        return int(exc.code or 0)
    action = getattr(arguments, "action", "")
    if not action:
        parser.print_help()
        return 2
    try:
        if action == "secret":
            # Printed once, for the operator to paste into RE0_SESSION_SECRET. It is not stored here
            # and not echoed anywhere else: a secret in a log is not a secret.
            print(new_secret())
            print("把它设为 RE0_SESSION_SECRET；只会打印这一次，不会写进任何文件", file=sys.stderr)
            return 0
        deployment = from_env()
        if not deployment.hosted:
            print(f"当前是本地模式（RE0_MODE={deployment.mode or 'local'}）：所有数据属于唯一的所有者 "
                  f"{LOCAL_OWNER!r}，没有账户也没有登录这一步。\n"
                  "要开账户，请先把 RE0_MODE=hosted 和它要求的配置一起设好——托管模式缺配置会拒绝启动，"
                  "不会静默降级。", file=sys.stderr)
            return 2
        store = _store(arguments.db)
        if action == "create-user":
            password = _read_password(arguments)
            account = store.create_account(arguments.username, password)
            print(f"已创建账户 {account['username']}")
            print(f"  user_id   {account['user_id']}")
            print(f"  workspace {account['workspace']}（该账户所有数据的归属标识）")
            print("口令没有存储为明文，也没有打印；忘记口令只能由操作者重建账户。")
            return 0
        if action == "list":
            rows = store.accounts()
            if not rows:
                print("还没有账户。托管模式下没有账户就没人能登录——这是拒绝服务的状态，不是漏洞。")
                return 0
            for row in rows:
                state = "已停用" if row["disabled"] else ("锁定至 " + row["locked_until"]
                                                          if row["locked_until"] else "可用")
                print(f"{row['username']:<24} {row['user_id']}  {row['workspace']}  {state}"
                      f"  创建于 {row['created_at']}")
            return 0
        if action == "disable":
            user_id = _resolve(store, arguments)
            store.set_disabled(user_id, not arguments.enable)
            revoked = store.revoke_user(user_id)
            print(("已启用" if arguments.enable else "已停用") + f"账户 {user_id}；"
                  f"同时失效 {revoked} 个进行中的会话")
            return 0
        if action == "revoke":
            user_id = _resolve(store, arguments)
            print(f"已失效 {store.revoke_user(user_id)} 个会话；账户本身仍可用于重新登录")
            return 0
        if action == "purge":
            print(f"已清理 {store.purge_expired()} 个过期或已失效的会话行")
            return 0
    except (AuthError, DeploymentError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    parser.print_help()
    return 2


def _resolve(store: AccountStore, arguments) -> str:
    """An account by id or by name. Naming both, or neither, is a mistake worth refusing."""
    if bool(arguments.user_id) == bool(arguments.username):
        raise AuthError("请给出 --user-id 或 --username 中的一个")
    if arguments.user_id:
        return arguments.user_id
    match = [row for row in store.accounts() if row["username"] == arguments.username.strip().lower()]
    if not match:
        raise AuthError(f"没有这个用户名：{arguments.username}")
    return match[0]["user_id"]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog=PROGRAM, description=__doc__.splitlines()[0])
    actions = parser.add_subparsers(dest="action")

    actions.add_parser("secret", help="print a new RE0_SESSION_SECRET (once, to stdout)")

    create = actions.add_parser("create-user",
                                help="provision one account; the password is never a flag")
    create.add_argument("--username", required=True)
    create.add_argument("--password-stdin", action="store_true",
                        help="read the password from one line of stdin instead of prompting")
    create.add_argument("--db", default="", help="library database path (default: $RE0_DB)")

    listing = actions.add_parser("list", help="accounts and their state; no secret is printed")
    listing.add_argument("--db", default="")

    for name, help_text in (("disable", "stop an account and end its live sessions"),
                            ("revoke", "end an account's live sessions, keep the account")):
        action = actions.add_parser(name, help=help_text)
        action.add_argument("--user-id", default="")
        action.add_argument("--username", default="")
        action.add_argument("--enable", action="store_true", help="with disable: re-enable instead")
        action.add_argument("--db", default="")

    purge = actions.add_parser("purge", help="drop session rows that can no longer be used")
    purge.add_argument("--db", default="")
    return parser


if __name__ == "__main__":
    raise SystemExit(main())
