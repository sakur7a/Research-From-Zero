"""Which kind of service this is, and what that kind must prove before it starts.

Re0 has always been a single-user loopback app, and every defence in it was built on that
assumption: the CSRF and origin rules are *not* authorization, the model vault holds one
configuration for whoever is at the keyboard, and one busy flag serializes the whole process.
Nothing about that is wrong for a local tool, and nothing about it survives being reachable.

So the mode is explicit, and the two modes fail differently. `local` is the default and needs no
configuration, because there is nobody to configure it for. `hosted` refuses to start until the
operator has supplied a session secret, a public HTTPS entry, allowed origins and an explicit storage
contract —
**every** missing piece is reported at once, rather than one per restart. The failure this exists to
prevent is the silent kind: a service written for loopback becoming public because someone changed
`RE0_HOST`, put a reverse proxy in front of it, or hid the settings button. None of those is a mode
change, and none of them makes the rest of this true.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from urllib.parse import urlsplit

MODES = ("local", "hosted")
STORAGE_MODES = ("ephemeral-demo", "persistent")
LOCAL_OWNER = "local"
MIN_SECRET_CHARS = 32
SESSION_COOKIE = "re0_session"
# A loopback origin on a public service is not a convenience: everybody has their own localhost, so
# allowing it lets any visitor's machine present a page the origin check accepts.
LOOPBACK_HOSTNAMES = {"127.0.0.1", "::1", "localhost"}
# A secret that appears in a repository, a tutorial or this file is public knowledge. Refusing the
# obvious ones is not a password policy; it stops a copied example from becoming a deployment.
WELL_KNOWN_SECRETS = ("changeme", "change-me", "secret", "re0-session-secret", "re0_session_secret",
                      "dev-secret", "development", "example", "placeholder", "password")


class DeploymentError(RuntimeError):
    """The configuration does not support the mode it asks for. Raised at startup, not per request."""


def documented_example(secret: str) -> bool:
    """A well-known token, or one repeated until it reaches the length limit.

    Repetition is worth catching on its own: "太短" invites an operator to pad the example they
    already typed rather than to generate a new one, and the padded form passes a length check.
    Checked before length so the diagnosis is "this value is public", not "this value is short".
    """
    low = secret.lower()
    return any(low and not low.replace(wk, "") for wk in WELL_KNOWN_SECRETS)


@dataclass(frozen=True)
class Deployment:
    """The answered question "who may reach this service, and how is it proven".

    `problems` is part of the value on purpose: a configuration is judged as a whole, so an operator
    sees every missing piece in one start-up instead of discovering them one restart at a time.
    """

    mode: str = "local"
    session_secret: str = ""
    public_entry: str = ""
    allowed_origins: tuple[str, ...] = ()
    storage_mode: str = "local"
    guest_access_enabled: bool = False
    problems: tuple[str, ...] = ()

    @property
    def hosted(self) -> bool:
        return self.mode == "hosted"

    @property
    def auth_required(self) -> bool:
        """Only a verified session speaks in hosted mode. Local mode has one implicit owner."""
        return self.hosted

    @property
    def startable(self) -> bool:
        return not self.problems

    @property
    def cookie_secure(self) -> bool:
        # Local mode is http on loopback, where `Secure` would drop the cookie entirely and log the
        # operator out of their own machine. Hosted mode declares an HTTPS entry, so it is required.
        return self.hosted

    def owner_for(self, identity) -> str:
        """The owner a row belongs to. Never taken from a request body."""
        return identity.owner if identity is not None and identity.authenticated else LOCAL_OWNER

    def origin_allowed(self, origin: str, host: str) -> bool:
        if not origin:
            return True
        try:
            parsed = urlsplit(origin)
        except ValueError:
            return False
        if parsed.scheme not in {"http", "https"}:
            return False
        if self.hosted:
            return origin.rstrip("/") in {item.rstrip("/") for item in self.allowed_origins}
        return parsed.netloc == host

    def describe(self) -> dict:
        """Public description for `/api/health` and startup output. Contains no secret."""
        return {"mode": self.mode, "auth_required": self.auth_required,
                "public_entry": self.public_entry, "allowed_origins": list(self.allowed_origins),
                "storage_mode": self.storage_mode,
                "guest_access_enabled": self.guest_access_enabled,
                "session_secret_configured": bool(self.session_secret),
                "problems": list(self.problems)}

    def require_startable(self) -> "Deployment":
        if self.mode not in MODES:
            raise DeploymentError(f"RE0_MODE 只能是 {' 或 '.join(MODES)}，收到 {self.mode!r}")
        if self.problems:
            raise DeploymentError("托管模式缺少必需配置，拒绝启动（不会静默降级为本地模式）：\n- "
                                  + "\n- ".join(self.problems))
        return self


def from_env(environ: dict | None = None) -> Deployment:
    """Read the mode and, for hosted, everything the mode promises.

    Problems accumulate instead of short-circuiting: a start-up that names one missing variable at a
    time turns configuration into a guessing game, and the guess is usually "then it must be fine".
    """
    env = os.environ if environ is None else environ
    mode = (env.get("RE0_MODE") or "local").strip().lower()
    if mode not in MODES:
        return Deployment(mode=mode, problems=(f"RE0_MODE 只能是 {' 或 '.join(MODES)}",))
    if mode == "local":
        # A local service still reads the origin allowlist, because an operator may run it behind a
        # loopback proxy on a non-default port. It never reads a session secret: nothing to sign.
        origins = _origins(env.get("RE0_ALLOWED_ORIGINS", ""))
        return Deployment(mode="local", allowed_origins=origins, storage_mode="local")

    problems: list[str] = []
    render = _render_enabled(env)
    secret = (env.get("RE0_SESSION_SECRET") or "").strip()
    if not secret:
        problems.append("RE0_SESSION_SECRET 未设置：托管模式需要一个至少 "
                        f"{MIN_SECRET_CHARS} 字符的随机会话密钥（用 `python -m re0 auth secret` 生成）")
    elif documented_example(secret):
        problems.append("RE0_SESSION_SECRET 是文档里出现过的示例值（含重复拼凑的变体）；请换成新生成的随机值")
    elif len(secret) < MIN_SECRET_CHARS:
        problems.append(f"RE0_SESSION_SECRET 太短（{len(secret)} < {MIN_SECRET_CHARS} 字符）")

    entry = (env.get("RE0_PUBLIC_ENTRY") or "").strip()
    if not entry and render:
        entry = _render_public_entry(env)
    if not entry:
        hint = "；Render 环境可使用平台提供的 RENDER_EXTERNAL_URL" if render else ""
        problems.append("RE0_PUBLIC_ENTRY 未设置：托管模式需要声明对外的 https 入口（TLS 终止在哪一层）" + hint)
    else:
        parsed = urlsplit(entry)
        if parsed.scheme != "https" or not parsed.netloc:
            problems.append(f"RE0_PUBLIC_ENTRY 必须是 https 网址，收到 {entry!r}")

    origins_raw = (env.get("RE0_ALLOWED_ORIGINS") or "").strip()
    if origins_raw:
        origins = _origins(origins_raw)
    elif render and entry:
        # Render owns these values. This is a convenience for the selected template, not a general
        # rule that guesses public origins from a request or an arbitrary Host header.
        origins = (entry.rstrip("/"),)
    else:
        origins = ()
    if not origins:
        hint = "；Render 环境可使用平台入口作为唯一来源" if render else ""
        problems.append("RE0_ALLOWED_ORIGINS 未设置：托管模式需要显式列出允许的来源（逗号分隔的 https 源）" + hint)
    elif entry:
        allowed = {item.rstrip("/") for item in origins}
        if entry.rstrip("/") not in allowed:
            problems.append(f"RE0_PUBLIC_ENTRY（{entry}）不在 RE0_ALLOWED_ORIGINS 里；"
                            "对外入口自己却被来源检查拒绝，说明两者有一处写错了")
        for item in origins:
            if not item.startswith("https://"):
                problems.append(f"允许的来源必须是 https：{item!r}")
            elif (urlsplit(item).hostname or "").lower() in LOOPBACK_HOSTNAMES:
                problems.append(f"托管模式不接受本机来源（{item!r}）：每个访问者都有自己的 localhost，"
                                "把它列进允许来源等于让任何一台机器都能带着会话发跨站请求")

    if env.get("RE0_ALLOW_INSECURE_COOKIES", "").strip().lower() in {"1", "true", "yes"}:
        problems.append("RE0_ALLOW_INSECURE_COOKIES 在托管模式下不被接受：会话 Cookie 必须是 Secure 的")

    storage_mode = (env.get("RE0_STORAGE_MODE") or "").strip().lower()
    if storage_mode not in STORAGE_MODES:
        if not storage_mode:
            problems.append("RE0_STORAGE_MODE 未设置：托管模式必须明确选择 ephemeral-demo（临时演示）或 persistent（持久卷）")
        else:
            problems.append(f"RE0_STORAGE_MODE 只能是 {' 或 '.join(STORAGE_MODES)}，收到 {storage_mode!r}")

    guest_access = (env.get("RE0_GUEST_ACCESS") or "").strip().lower() in {"1", "true", "yes", "on"}
    return Deployment(mode="hosted", session_secret=secret, public_entry=entry,
                      allowed_origins=origins, storage_mode=storage_mode,
                      guest_access_enabled=guest_access, problems=tuple(problems))


def trusted_hosts_from_env(environ: dict | None = None) -> tuple[str, ...]:
    """Host-header allowlist, with a Render hostname only in Render's declared environment.

    An explicit RE0_ALLOWED_HOSTS replaces the defaults. Render's platform hostname is trusted only
    when its documented RENDER flag is true; it is never learned from an inbound request.
    """
    env = os.environ if environ is None else environ
    explicit = env.get("RE0_ALLOWED_HOSTS")
    if explicit is not None:
        return tuple(item.strip() for item in explicit.split(",") if item.strip())

    allowed = ["localhost", "127.0.0.1", "[::1]", "testserver"]
    if _render_enabled(env):
        hostname = (env.get("RENDER_EXTERNAL_HOSTNAME") or "").strip()
        if not _valid_hostname(hostname):
            hostname = _hostname_from_url(env.get("RENDER_EXTERNAL_URL", ""))
        if hostname and hostname not in allowed:
            allowed.append(hostname)
    return tuple(allowed)


def _render_enabled(env: dict) -> bool:
    return (env.get("RENDER") or "").strip().lower() == "true"


def _render_public_entry(env: dict) -> str:
    """Use only Render's platform URL; ignore malformed values and let startup refuse safely."""
    raw_url = (env.get("RENDER_EXTERNAL_URL") or "").strip()
    if raw_url:
        try:
            parsed = urlsplit(raw_url)
            if (parsed.scheme == "https" and parsed.hostname and parsed.username is None
                    and parsed.password is None and parsed.path in {"", "/"}
                    and not parsed.query and not parsed.fragment):
                port = parsed.port
                return f"https://{parsed.hostname.lower()}" + (f":{port}" if port else "")
        except ValueError:
            pass
    hostname = (env.get("RENDER_EXTERNAL_HOSTNAME") or "").strip()
    return f"https://{hostname.lower()}" if _valid_hostname(hostname) else ""


def _hostname_from_url(raw_url: str) -> str:
    try:
        parsed = urlsplit(raw_url)
        if parsed.scheme == "https" and parsed.hostname and parsed.username is None and parsed.password is None:
            return parsed.hostname.lower()
    except ValueError:
        pass
    return ""


def _valid_hostname(raw: str) -> bool:
    try:
        parsed = urlsplit("//" + raw)
        return bool(parsed.hostname and parsed.port is None and not parsed.username and not parsed.password
                    and not parsed.path and not parsed.query and not parsed.fragment)
    except ValueError:
        return False


def _origins(raw: str) -> tuple[str, ...]:
    seen: list[str] = []
    for item in (raw or "").split(","):
        value = item.strip().rstrip("/")
        if value and value not in seen:
            seen.append(value)
    return tuple(seen)
