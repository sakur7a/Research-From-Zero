"""Accounts, sessions, and the one identity a request is allowed to have.

The rule this module exists to enforce is that **identity comes from a verified session and from
nowhere else**. A request body may not declare an owner, a query string may not name a workspace,
and a header may not assert a user: every one of those is attacker-controlled text, and reading an
owner out of a payload is how one user's export becomes another user's export. So the endpoints take
no owner argument at all — they are handed an `Identity` that was resolved from a session token, and
the owner is a property of it.

Three choices worth stating, because each trades something away:

* **Opaque tokens stored server-side, not signed tokens carried by the client.** A signed token
  cannot be revoked before it expires, and "logout must stop working immediately" is one of the
  release gates. The token is random, and only its SHA-256 is stored, so a leaked database does not
  hand out live sessions.
* **Accounts are provisioned by the operator on the server console**, never by an HTTP endpoint.
  Open registration on a service that holds other people's model keys is not a feature.
* **A lockout after repeated failures, per account.** Password verification is also run against a
  dummy hash when the username does not exist, so the response time does not enumerate accounts.

The local mode has no accounts at all: it has one implicit owner, `local`, which is a reserved name
precisely so that a hosted account can never inherit the rows a single-user database already holds.
"""
from __future__ import annotations

import hashlib
import hmac
import re
import secrets
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from .db import Database
from .deployment import LOCAL_OWNER

PBKDF2_ROUNDS = 210_000
SALT_BYTES = 16
MIN_PASSWORD_CHARS = 10
USERNAME_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_.-]{2,31}$")
SESSION_TTL = timedelta(hours=12)
MAX_FAILED_ATTEMPTS = 5
LOCKOUT = timedelta(minutes=15)
TOKEN_BYTES = 32
# One dummy so that "no such user" costs the same as "wrong password". Without it the timing
# difference is an account list.
_DUMMY_HASH = "pbkdf2_sha256$210000$" + "AA" * SALT_BYTES + "$" + "AB" * 32

AUTH_SCHEMA = """
CREATE TABLE IF NOT EXISTS auth_schema_version (version INTEGER NOT NULL);
INSERT INTO auth_schema_version SELECT 1 WHERE NOT EXISTS (SELECT 1 FROM auth_schema_version);
CREATE TABLE IF NOT EXISTS auth_accounts (
  user_id TEXT PRIMARY KEY,
  username TEXT NOT NULL UNIQUE,
  password_hash TEXT NOT NULL,
  workspace TEXT NOT NULL,
  created_at TEXT NOT NULL,
  disabled INTEGER NOT NULL DEFAULT 0,
  failed_count INTEGER NOT NULL DEFAULT 0,
  locked_until TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS auth_sessions (
  token_hash TEXT PRIMARY KEY,
  user_id TEXT NOT NULL REFERENCES auth_accounts(user_id) ON DELETE CASCADE,
  created_at TEXT NOT NULL,
  expires_at TEXT NOT NULL,
  revoked_at TEXT NOT NULL DEFAULT '',
  note TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS auth_sessions_user ON auth_sessions(user_id, expires_at);
"""


class AuthError(RuntimeError):
    """A refusal with a message that can be shown to a person and leaks nothing."""


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _parse(value: str) -> datetime | None:
    try:
        return datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None


def hash_password(password: str, *, salt: bytes | None = None, rounds: int = PBKDF2_ROUNDS) -> str:
    salt = salt or secrets.token_bytes(SALT_BYTES)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, rounds)
    return f"pbkdf2_sha256${rounds}${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    """Constant-time, and total: a malformed stored hash answers False instead of raising."""
    try:
        algorithm, rounds, salt, digest = stored.split("$")
        if algorithm != "pbkdf2_sha256":
            return False
        expected = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt),
                                       int(rounds)).hex()
    except (ValueError, AttributeError):
        return False
    return hmac.compare_digest(expected, digest)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


@dataclass(frozen=True)
class Identity:
    """Who is asking. Built by `AccountStore.resolve`, or the implicit owner in local mode."""

    user_id: str
    workspace: str
    authenticated: bool
    expires_at: str = ""

    @property
    def owner(self) -> str:
        """The string stored on every row this identity owns.

        The workspace, not the user id, so that a row keeps its owner if an account is ever renamed;
        and never a value read from a request.
        """
        return self.workspace

    def public(self) -> dict:
        return {"user_id": self.user_id, "workspace": self.workspace,
                "authenticated": self.authenticated, "expires_at": self.expires_at}


def local_identity() -> Identity:
    """The one identity a single-user local service has. Not authenticated — nobody asked it to be."""
    return Identity(user_id=LOCAL_OWNER, workspace=LOCAL_OWNER, authenticated=False)


def anonymous_identity() -> Identity:
    """Nobody, owning nothing.

    The empty workspace matches no row, so a route that forgets to require a session serves an empty
    library instead of the local owner's. Falling back to `local_identity()` here would be the
    convenient mistake: the migrated single-user data is exactly what a logged-out visitor must not
    be handed.
    """
    return Identity(user_id="", workspace="", authenticated=False)


class AccountStore:
    """Accounts and sessions, in the same database as everything else they authorize.

    Sharing the file is deliberate: a session that outlives the database it grants access to, or a
    backup that holds one without the other, is a confusion nobody needs. No model key is stored
    here or anywhere else in the database — see `agent/model.py`.
    """

    SCHEMA_VERSION = 1

    def __init__(self, db: Database):
        self.db = db
        with self.db.connect() as con:
            con.executescript(AUTH_SCHEMA)
            version = con.execute("SELECT version FROM auth_schema_version").fetchone()[0]
        if version > self.SCHEMA_VERSION:
            raise RuntimeError(f"Unsupported auth schema: {version}; no destructive migration "
                               "was performed")

    # --- accounts

    def count_accounts(self) -> int:
        with self.db.connect() as con:
            return con.execute("SELECT COUNT(*) FROM auth_accounts").fetchone()[0]

    def accounts(self) -> list[dict]:
        with self.db.connect() as con:
            rows = con.execute("SELECT user_id, username, workspace, created_at, disabled, "
                               "locked_until FROM auth_accounts ORDER BY created_at").fetchall()
        return [{"user_id": row[0], "username": row[1], "workspace": row[2], "created_at": row[3],
                 "disabled": bool(row[4]), "locked_until": row[5]} for row in rows]

    def create_account(self, username: str, password: str, *, note: str = "") -> dict:
        """Provision one account. Called from the server console, never from a request."""
        name = (username or "").strip().lower()
        if not USERNAME_PATTERN.match(name):
            raise AuthError("用户名只能是 3–32 位小写字母、数字、点、下划线或连字符，且以字母或数字开头")
        if name == LOCAL_OWNER or name.startswith("local"):
            # `local` owns every row a single-user database already holds. An account that could
            # take that name would inherit a stranger's library, notes and evidence.
            raise AuthError(f"{LOCAL_OWNER!r} 是保留名：它属于迁移前的本地数据，不能被新账户继承")
        if len(password or "") < MIN_PASSWORD_CHARS:
            raise AuthError(f"口令至少 {MIN_PASSWORD_CHARS} 个字符")
        user_id = "usr_" + secrets.token_hex(8)
        workspace = "ws_" + secrets.token_hex(8)
        try:
            with self.db.connect() as con:
                con.execute("INSERT INTO auth_accounts(user_id,username,password_hash,workspace,"
                            "created_at) VALUES (?,?,?,?,?)",
                            (user_id, name, hash_password(password), workspace, _now()))
        except sqlite3.IntegrityError as exc:
            raise AuthError("此用户名已存在") from exc
        return {"user_id": user_id, "username": name, "workspace": workspace, "note": note}

    def set_disabled(self, user_id: str, disabled: bool) -> bool:
        with self.db.connect() as con:
            changed = con.execute("UPDATE auth_accounts SET disabled=? WHERE user_id=?",
                                  (int(disabled), user_id)).rowcount
            if changed:
                # A disabled account must not keep working through a session it already has.
                con.execute("UPDATE auth_sessions SET revoked_at=? WHERE user_id=? AND revoked_at=''",
                            (_now(), user_id))
        return bool(changed)

    # --- sessions

    def verify(self, username: str, password: str) -> str | None:
        """The user id behind a correct password, or None. Locks an account after repeated failures.

        The same verification work is done for an unknown username as for a known one, so the answer
        does not arrive faster when the account does not exist.
        """
        name = (username or "").strip().lower()
        with self.db.connect() as con:
            row = con.execute("SELECT user_id, password_hash, disabled, failed_count, locked_until "
                              "FROM auth_accounts WHERE username=?", (name,)).fetchone()
            record = tuple(row) if row is not None else None
        stored = record[1] if record else _DUMMY_HASH
        correct = verify_password(password or "", stored)
        if record is None:
            return None
        user_id, _, disabled, failures, locked_until = record
        locked = _parse(locked_until) if locked_until else None
        # Checked after the hash, so a locked account and a wrong password cost the same.
        if locked is not None and locked > datetime.now(timezone.utc):
            return None
        if disabled:
            return None
        if not correct:
            failures += 1
            until = ""
            if failures >= MAX_FAILED_ATTEMPTS:
                until = (datetime.now(timezone.utc) + LOCKOUT).replace(microsecond=0).isoformat()
                failures = 0
            with self.db.connect() as con:
                con.execute("UPDATE auth_accounts SET failed_count=?, locked_until=? WHERE user_id=?",
                            (failures, until, user_id))
            return None
        if failures or locked_until:
            with self.db.connect() as con:
                con.execute("UPDATE auth_accounts SET failed_count=0, locked_until='' WHERE user_id=?",
                            (user_id,))
        return user_id

    def issue(self, user_id: str, *, ttl: timedelta = SESSION_TTL, note: str = "") -> tuple[str, str]:
        """A new session token and when it expires. The token is returned once and stored hashed."""
        token = secrets.token_urlsafe(TOKEN_BYTES)
        expires = (datetime.now(timezone.utc) + ttl).replace(microsecond=0).isoformat()
        with self.db.connect() as con:
            if not con.execute("SELECT 1 FROM auth_accounts WHERE user_id=? AND disabled=0",
                               (user_id,)).fetchone():
                raise AuthError("账户不存在或已停用")
            con.execute("INSERT INTO auth_sessions(token_hash,user_id,created_at,expires_at,note) "
                        "VALUES (?,?,?,?,?)", (hash_token(token), user_id, _now(), expires, note[:200]))
        return token, expires

    def resolve(self, token: str) -> Identity | None:
        """The identity behind a session token, or None if it is unknown, expired or revoked."""
        if not token:
            return None
        digest = hash_token(token)
        with self.db.connect() as con:
            row = con.execute("SELECT s.user_id, s.expires_at, s.revoked_at, a.workspace, a.disabled "
                              "FROM auth_sessions s JOIN auth_accounts a ON a.user_id=s.user_id "
                              "WHERE s.token_hash=?", (digest,)).fetchone()
            if row is None:
                return None
            user_id, expires_at, revoked_at, workspace, disabled = tuple(row)
            if revoked_at or disabled:
                return None
            expires = _parse(expires_at)
            if expires is None or expires <= datetime.now(timezone.utc):
                # An expired session is revoked rather than left to be resolved again and again: the
                # row now says when it stopped being usable.
                con.execute("UPDATE auth_sessions SET revoked_at=? WHERE token_hash=? AND revoked_at=''",
                            (_now(), digest))
                return None
        return Identity(user_id=user_id, workspace=workspace, authenticated=True,
                        expires_at=expires_at)

    def revoke(self, token: str) -> bool:
        with self.db.connect() as con:
            result = con.execute("UPDATE auth_sessions SET revoked_at=? WHERE token_hash=? "
                                 "AND revoked_at=''", (_now(), hash_token(token)))
        return bool(result.rowcount)

    def revoke_user(self, user_id: str) -> int:
        with self.db.connect() as con:
            return con.execute("UPDATE auth_sessions SET revoked_at=? WHERE user_id=? AND revoked_at=''",
                               (_now(), user_id)).rowcount

    def purge_expired(self) -> int:
        """Drop sessions that can no longer be used. Revoked rows are dropped with them."""
        with self.db.connect() as con:
            return con.execute("DELETE FROM auth_sessions WHERE expires_at < ? OR revoked_at != ''",
                               (_now(),)).rowcount

    def sessions(self, user_id: str) -> list[dict]:
        """Live sessions for one account. Token hashes are never returned; nothing here is a secret
        an operator needs, and a hash is still a hash."""
        with self.db.connect() as con:
            rows = con.execute("SELECT created_at, expires_at, note FROM auth_sessions WHERE "
                               "user_id=? AND revoked_at='' AND expires_at > ? ORDER BY created_at",
                               (user_id, _now())).fetchall()
        return [{"created_at": row[0], "expires_at": row[1], "note": row[2]} for row in rows]


def new_secret(nbytes: int = 48) -> str:
    """A session secret for `RE0_SESSION_SECRET`, printed once by `re0 auth secret`."""
    return secrets.token_urlsafe(nbytes)
