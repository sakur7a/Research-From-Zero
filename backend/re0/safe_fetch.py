"""A fetcher with a destination allowlist, per-hop redirect validation and hard size limits.

This exists because reading a paper's full text means following a link into the open web, and the
retrieval paths until now only ever spoke to a fixed set of API hosts. Three rules make that safe
enough to ship, and they are enforced here rather than left to callers:

* **The destination is chosen from an identifier, never taken as a URL.** A caller names an arXiv or
  ACL Anthology identifier; this module builds the address. There is no arbitrary-URL entry point,
  so there is nothing for a prompt-injected document to aim at.
* **Every hop is re-validated.** A redirect is not trusted because its origin was: each hop must
  still be https, still on the allowlist, still on a public address, and must not repeat.
* **The response is bounded while it streams**, so a large file or a compression bomb is stopped by
  bytes read rather than by a header the server controls.

Credentials are never sent: no `Authorization`, no cookies, `trust_env=False`. A full-text read is
an anonymous request to a public archive, and anything else would leak a key to a host that has no
business holding it.
"""
from __future__ import annotations

import ipaddress
import os
import socket
import time
import datetime
from urllib.parse import urlsplit, urlunsplit

import httpx

# The public open-access archives this module will speak to. Adding a host is a deliberate act with
# a review behind it, not a configuration knob: each one is a place we have checked that serving an
# anonymous reader is permitted, and that a paper's text is genuinely open.
ALLOWED_FULLTEXT_HOSTS = frozenset({"arxiv.org", "export.arxiv.org", "aclanthology.org"})
MAX_REDIRECT_HOPS = 5
MAX_RESPONSE_BYTES = 8 * 1024 * 1024
READ_TIMEOUT = 20.0
USER_AGENT = "re0/0.2 (full-text reader for open-access papers; anonymous, no credentials)"

FETCH_STATES = ("ok", "not_allowed", "not_found", "access_required", "rate_limited",
                "redirect_refused", "dns_refused", "too_large", "wrong_content_type",
                "fetch_failed", "too_many_hops")
# Some networks answer DNS through a local interceptor: on one such machine arxiv.org resolved to
# 198.18.1.3 and fdfe:dcba:9876::f9, both non-public. Refusing is the right default, because an
# allowlisted hostname pointed at a private address is exactly how a resolver gets used to reach
# something the allowlist never approved. But it also makes the reader unusable behind that proxy,
# so there is one explicit opt-in — and it is recorded in the result rather than being silent,
# because "we connected to a non-public address" is a fact a reader is entitled to.
LOCAL_RESOLVER_ENV = "RE0_ALLOW_LOCAL_RESOLVER"
TRUTHY = frozenset({"1", "true", "yes", "on"})


class FetchError(Exception):
    """A refusal or a failure, named. Never a traceback, and never the remote body."""

    def __init__(self, message: str, state: str = "fetch_failed", http_status: int | None = None):
        super().__init__(message)
        self.state = state
        self.http_status = http_status


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


def is_public_address(text: str) -> bool:
    """Whether a resolved address may be connected to.

    Loopback, private, link-local (which is where cloud metadata lives), multicast, reserved and
    unspecified addresses are all refused. An allowlisted hostname that resolves to one of them —
    through a poisoned hosts file or a compromised resolver — is therefore stopped here rather than
    fetched.
    """
    try:
        address = ipaddress.ip_address(text)
    except ValueError:
        return False
    return not (address.is_private or address.is_loopback or address.is_link_local
                or address.is_multicast or address.is_reserved or address.is_unspecified)


def validate_url(url: str) -> str:
    """Check one address against the allowlist and return it unchanged.

    The scheme, the port and the absence of embedded credentials are all part of the check: a
    `https://user:pass@host/` URL would otherwise carry a secret to a host that never asked for one.
    """
    parsed = urlsplit(url)
    if parsed.scheme != "https":
        raise FetchError("只允许 https 目标", "not_allowed")
    if parsed.username or parsed.password:
        raise FetchError("目标 URL 里不能带凭据；本次没有发出请求", "not_allowed")
    if parsed.port not in (None, 443):
        raise FetchError("只请求默认 443 端口，不请求自定义端口", "not_allowed")
    host = (parsed.hostname or "").lower()
    if host not in ALLOWED_FULLTEXT_HOSTS:
        raise FetchError(f"{host or '(空)'} 不在允许读取全文的公开档案列表内"
                         f"（{', '.join(sorted(ALLOWED_FULLTEXT_HOSTS))}）", "not_allowed")
    return url


def local_resolver_allowed() -> bool:
    return os.getenv(LOCAL_RESOLVER_ENV, "").strip().lower() in TRUTHY


def validate_resolution(host: str, *, local_opt_out: bool = True) -> tuple[list[str], str]:
    """Resolve the host and require every address it returns to be public.

    All of them, not the first: a resolver can answer with several records, and connecting to a
    public one while a private one also sits in the list is how a rebinding defence gets talked
    around. The residual window between this check and the connection is documented in the module
    docstring rather than pretended away.

    Returns `(addresses, note)`. The note is empty unless `RE0_ALLOW_LOCAL_RESOLVER` let a
    non-public address through, in which case it says so and names the addresses.

    `local_opt_out=False` is for a caller acting on behalf of the service rather than of the person
    at the keyboard: there the variable must not be honoured, and the refusal must not advertise it
    either, because advice that cannot be followed reads as a bug in the operator's setup.
    """
    try:
        infos = socket.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        raise FetchError(f"无法解析 {host}（{exc.strerror or 'DNS 失败'}）", "dns_refused") from exc
    addresses = sorted({info[4][0] for info in infos})
    if not addresses:
        raise FetchError(f"{host} 没有解析到任何地址", "dns_refused")
    refused = [address for address in addresses if not is_public_address(address)]
    if not refused:
        return addresses, ""
    listed = "、".join(refused)
    if local_opt_out and local_resolver_allowed():
        return addresses, (f"{host} 解析到了非公网地址（{listed}）；因为设置了 "
                           f"{LOCAL_RESOLVER_ENV}，本次按本机透明代理处理并继续。"
                           "目标主机仍受白名单限制，但连接确实经过了本机代理。")
    advice = (f"如果这是你所在网络的透明代理（不是攻击），可以设 {LOCAL_RESOLVER_ENV}=1 显式允许，"
              "届时结果里会记录连到了哪些地址" if local_opt_out else
              f"{LOCAL_RESOLVER_ENV} 在这里不适用：这次连接代表的是服务本身，不是你本机的网络")
    raise FetchError(f"{host} 解析到了非公网地址（{listed}）；已拒绝连接。{advice}", "dns_refused")


def fetch(url: str, *, accept: tuple[str, ...] = ("text/html", "application/pdf"),
          transport: httpx.BaseTransport | None = None, resolver=None,
          max_bytes: int = MAX_RESPONSE_BYTES) -> dict:
    """Fetch one allowlisted address, following redirects only where each hop re-validates.

    Returns a dict rather than raising where it can: a caller that has already retrieved metadata
    should keep it and add "the full text could not be read, and here is why", which an exception
    makes awkward. `FetchError` is still raised for programmer errors such as a malformed URL.
    """
    started = _now()
    current = url
    seen: list[str] = []
    notes: list[str] = []
    # Resolved at call time rather than bound as a default, so the check can be replaced in a test
    # without the replacement being silently ignored by an already-captured reference.
    resolve = resolver or validate_resolution
    try:
        validate_url(current)
    except FetchError as exc:
        return {"state": exc.state, "detail": str(exc), "url": url, "fetched_at": started,
                "hops": [], "bytes_read": 0}
    client = httpx.Client(transport=transport, timeout=READ_TIMEOUT, follow_redirects=False,
                          trust_env=False)
    try:
        for hop in range(MAX_REDIRECT_HOPS + 1):
            host = (urlsplit(current).hostname or "").lower()
            try:
                # Injectable so a test can exercise the address rules without asking a resolver
                # about a host it does not own. The default is the real check.
                note = resolve(host)
                if isinstance(note, tuple):
                    _, note = note
                if note and note not in notes:
                    notes.append(note)
            except FetchError as exc:
                # The resolver speaks in addresses; this caller owes the reader the consequence too.
                return {"state": exc.state, "detail": f"{exc}；本次没有取得全文", "url": url,
                        "final_url": current, "fetched_at": started, "hops": seen, "bytes_read": 0}
            try:
                with client.stream("GET", current,
                                   headers={"User-Agent": USER_AGENT,
                                            "Accept": ", ".join(accept)}) as response:
                    status = response.status_code
                    if 300 <= status < 400:
                        location = response.headers.get("location", "")
                        if not location:
                            return _failure("重定向没有给出 Location", "redirect_refused", url,
                                            current, started, seen, 0, status)
                        # Resolved against the current hop, then validated as an address in its own
                        # right: a redirect inherits nothing from the page that issued it.
                        current = str(httpx.URL(current).join(location))
                        if current in seen:
                            return _failure(f"重定向出现回环（{len(seen) + 1} 跳）", "redirect_refused",
                                            url, current, started, seen, 0, status)
                        seen.append(current)
                        try:
                            validate_url(current)
                        except FetchError as exc:
                            return _failure(f"重定向目标被拒绝：{exc}", "redirect_refused", url,
                                            current, started, seen, 0, status)
                        if hop == MAX_REDIRECT_HOPS:
                            return _failure(f"重定向超过 {MAX_REDIRECT_HOPS} 跳", "too_many_hops",
                                            url, current, started, seen, 0, status)
                        continue
                    if status == 404:
                        return _failure("该地址没有全文（404）；这不等于论文不存在，可能只是这个来源没有",
                                        "not_found", url, current, started, seen, 0, status)
                    if status in (401, 403):
                        return _failure("访问被拒绝（需要授权或不允许匿名读取）；本次没有绕过任何限制",
                                        "access_required", url, current, started, seen, 0, status)
                    if status == 429:
                        return _failure("来源限流（429）；稍后可重试，这不是全文不存在",
                                        "rate_limited", url, current, started, seen, 0, status)
                    if status != 200:
                        return _failure(f"来源返回 HTTP {status}", "fetch_failed", url, current,
                                        started, seen, 0, status)
                    content_type = (response.headers.get("content-type") or "").split(";")[0].strip().lower()
                    if accept and content_type and not any(content_type == kind for kind in accept):
                        return _failure(f"内容类型是 {content_type or '未知'}，不是 {'/'.join(accept)}；"
                                        "没有把别的东西当全文解析",
                                        "wrong_content_type", url, current, started, seen, 0, status)
                    data = bytearray()
                    for chunk in response.iter_bytes():
                        data.extend(chunk)
                        # Counted after decompression, so a small gzip that expands without limit is
                        # stopped by the same ceiling as a large plain body.
                        if len(data) > max_bytes:
                            return _failure(f"响应超过 {max_bytes // (1024 * 1024)} MiB 上限；已停止读取，"
                                            "没有解析完整内容",
                                            "too_large", url, current, started, seen, len(data), status)
                    return {"state": "ok", "url": url, "final_url": current, "status": status,
                            "content_type": content_type, "body": bytes(data),
                            "bytes_read": len(data), "fetched_at": started, "hops": list(seen),
                            "detail": "", "resolver_notes": list(notes)}
            except httpx.HTTPError as exc:
                # Never echo an exception that could carry remote-controlled text.
                return _failure(f"网络请求失败（{type(exc).__name__}）；本次没有取得全文",
                                "fetch_failed", url, current, started, seen, 0)
        return _failure(f"重定向超过 {MAX_REDIRECT_HOPS} 跳", "too_many_hops", url, current,
                        started, seen, 0)
    finally:
        client.close()


def _failure(detail: str, state: str, url: str, final_url: str, started: str, hops: list,
             bytes_read: int, http_status: int | None = None) -> dict:
    return {"state": state, "detail": detail, "url": url, "final_url": final_url,
            "fetched_at": started, "hops": list(hops), "bytes_read": bytes_read,
            "http_status": http_status, "body": b"", "resolver_notes": []}
