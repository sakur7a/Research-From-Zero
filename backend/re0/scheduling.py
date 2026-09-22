"""Bounded request scheduling: shared budgets, provider retry windows, cache TTL, pagination.

Every limit here exists so that a retrieval session can be *described* afterwards: how many
requests it made, which pages it reached, why it stopped, and what it deliberately did not
re-fetch. An exhausted budget says so out loud. It never degrades into an empty result set,
because "we stopped asking" and "there was nothing there" are different claims and a reader
cannot recover the difference from a list length.

Two kinds of stop are kept apart throughout:

* ours — `BudgetExhausted`, `Cancelled`: this run chose to stop, or was told to;
* theirs — `RateLimited`, `ProviderFailure`: the service declined or answered badly.

Collapsing them would let a rate limit read as a negative finding.
"""
from __future__ import annotations

import datetime
import hashlib
import re
import threading
import time
from dataclasses import dataclass, field
from email.utils import parsedate_to_datetime

# --- exceptions -------------------------------------------------------------


class Cancelled(Exception):
    """The caller asked to stop. Nothing scheduled after this runs."""


class BudgetExhausted(Exception):
    """The shared request budget is spent. A statement about this run, not about whether the
    source has more to give."""


class RateLimited(Exception):
    """The provider declined to answer right now and said when to come back."""


class ProviderFailure(Exception):
    """The provider answered, and the answer cannot be used."""


# --- pagination -------------------------------------------------------------

# `complete` is the only stop that means the source ran out of pages. Everything else means
# this run stopped first, and `truncated` is set so a caller cannot mistake one for the other.
PAGINATION_STOPS = ("complete", "empty_page", "cursor_missing", "page_budget", "request_budget",
                    "cancelled", "cursor_repeated", "cursor_rejected", "rate_limited",
                    "provider_failed", "not_supported")

# Only these two document their paging well enough to verify publicly: OpenAlex with an opaque
# `cursor`, Semantic Scholar with an `offset`. The rest are declared unsupported rather than
# paged on a guess — a guess that silently stops early is worse than an honest "one page".
PAGINATED_SOURCES = ("openalex", "semanticscholar", "zotero")
PAGINATION_NOTES = {
    "openalex": "OpenAlex 文档化的 cursor 分页（cursor=* 起，读 meta.next_cursor）",
    "semanticscholar": "Semantic Scholar 文档化的 offset 分页（offset+limit，单页上限 100）",
    "zotero": "Zotero 文档化的 start+limit 分页，配 Total-Results 与 Last-Modified-Version 响应头",
}
PAGINATION_UNSUPPORTED = ("{}：该来源的分页方式没有可公开核对的文档，本次只读一页。"
                          "未分页不等于已查全，也不等于没有更多结果")

# A cursor is returned by a remote service, so it is untrusted input. The guarantee is not that
# it looks harmless but that it can only ever become a query-parameter *value* on an
# already-allowlisted URL: it cannot reach the scheme, the host or the path, so no cursor can
# steer the next request somewhere the allowlist was never asked about. The charset below is the
# second line of defence, sized for the values these services really send (OpenAlex returns
# base64 of a JSON array, which legitimately contains `/` and `+`; Semantic Scholar returns an
# integer), so a path-shaped string is not distinguishable by shape alone and is not pretended
# to be. Whitespace, a `?`, a `#` and anything over-length are refused outright.
CURSOR_PATTERN = re.compile(r"^[A-Za-z0-9_.~:+/=,-]{1,512}$")

MAX_ATTEMPTS = 3            # one try plus two bounded retries; never nested or unbounded
BACKOFF_BASE = 0.4
BACKOFF_CAP = 6.0
RETRY_AFTER_CAP = 120.0     # a provider may ask for an hour; this run will not wait that long
DEFAULT_CACHE_TTL = 900.0


@dataclass
class PageOutcome:
    """What one paginated fetch actually did."""
    items: list = field(default_factory=list)
    pages_fetched: int = 0
    requests_used: int = 0
    next_cursor: str = ""
    truncated: bool = False
    stop_reason: str = "complete"
    detail: str = ""

    def as_dict(self) -> dict:
        return {"pages_fetched": self.pages_fetched, "requests_used": self.requests_used,
                "records": len(self.items), "next_cursor": self.next_cursor,
                "truncated": self.truncated, "stop_reason": self.stop_reason, "detail": self.detail}


def paginate(fetch, *, governor: "Governor | None" = None, provider: str, host: str = "",
             max_pages: int = 1, label: str = "") -> PageOutcome:
    """Drive `fetch(cursor) -> (items, next_cursor)` under the shared budget.

    `fetch` performs the network call; this function owns the stopping rules, so the rules are
    the same for every paginated source instead of being re-invented per connector. A source
    whose paging is not documented is read for exactly one page and says so, which is the
    honest version of "we did not page" — the alternative is a result set that looks complete.

    `governor=None` means no shared scheduler (a single bounded call), so nothing is accounted
    against a session budget; the stopping rules still apply.

    `provider` names the source, and decides whether paging is offered at all. `host` names the
    network destination the governor accounts against. They are different namespaces and must not
    be conflated: a rate limit attaches to a host, while documented pagination attaches to a
    source, and keying both by one string quietly double-counts the budget.
    """
    outcome = PageOutcome()
    destination = host or provider
    if max_pages <= 0:
        outcome.stop_reason = "page_budget"
        outcome.detail = "max_pages=0；本次没有发起任何分页请求"
        return outcome
    unsupported = provider not in PAGINATED_SOURCES

    def take() -> None:
        if governor is not None:
            governor.ensure_available(destination)

    def spent_before() -> int:
        return governor.requests_used if governor is not None else outcome.requests_used

    def one_page(cursor: str):
        before = spent_before()
        items, next_cursor = fetch(cursor)
        outcome.requests_used += (spent_before() if governor is not None else before + 1) - before
        outcome.pages_fetched += 1
        return list(items or []), str(next_cursor or "").strip()

    if unsupported:
        try:
            take()
            items, _ = one_page("")
        except Cancelled:
            outcome.stop_reason, outcome.detail = "cancelled", "调用已取消；没有发起请求"
            return outcome
        except BudgetExhausted as exc:
            outcome.stop_reason, outcome.truncated, outcome.detail = "request_budget", True, str(exc)
            return outcome
        except RateLimited as exc:
            outcome.stop_reason, outcome.truncated, outcome.detail = "rate_limited", True, str(exc)
            return outcome
        except ProviderFailure as exc:
            outcome.stop_reason, outcome.detail = "provider_failed", str(exc)
            return outcome
        outcome.items = items
        outcome.stop_reason, outcome.truncated = "not_supported", True
        outcome.detail = PAGINATION_UNSUPPORTED.format(label or provider)
        return outcome

    seen = {""}
    cursor = ""
    while True:
        if outcome.pages_fetched >= max_pages:
            outcome.stop_reason, outcome.truncated, outcome.next_cursor = "page_budget", True, cursor
            outcome.detail = f"已达到 max_pages={max_pages}；来源还有后续页未读取"
            return outcome
        try:
            take()
            items, next_cursor = one_page(cursor)
        except Cancelled:
            outcome.stop_reason, outcome.detail = "cancelled", "调用已取消；不再调度新的分页请求"
            return outcome
        except BudgetExhausted as exc:
            outcome.stop_reason, outcome.truncated, outcome.next_cursor = "request_budget", True, cursor
            outcome.detail = str(exc)
            return outcome
        except RateLimited as exc:
            outcome.stop_reason, outcome.truncated, outcome.next_cursor = "rate_limited", True, cursor
            outcome.detail = str(exc)
            return outcome
        except ProviderFailure as exc:
            # A page that fails after earlier pages succeeded keeps them: the run is partial,
            # and discarding what was read would report less than was actually established.
            outcome.stop_reason = "provider_failed"
            outcome.truncated = outcome.pages_fetched > 0
            outcome.next_cursor = cursor if outcome.truncated else ""
            outcome.detail = str(exc)
            return outcome
        outcome.items.extend(items)
        if not items:
            # An empty page is an answer, not a failure: the source was reached and had nothing
            # more. Still distinguished from `complete`, which means a cursor said so.
            outcome.stop_reason = "empty_page"
            outcome.detail = "该页没有返回记录；停止分页（来源已连通，这不是失败）"
            return outcome
        if not next_cursor:
            outcome.stop_reason = "complete"
            outcome.detail = "来源没有给出下一页游标；已读取的页就是它愿意提供的全部"
            return outcome
        if not CURSOR_PATTERN.fullmatch(next_cursor):
            outcome.stop_reason, outcome.truncated = "cursor_rejected", True
            outcome.next_cursor = ""
            outcome.detail = ("来源返回的游标含有不可接受的字符；已拒绝转发。"
                              "游标是不可信输入，不能改变请求白名单")
            return outcome
        if next_cursor in seen:
            outcome.stop_reason, outcome.truncated, outcome.next_cursor = "cursor_repeated", True, cursor
            outcome.detail = "来源重复返回同一个游标；停止分页以避免无限循环"
            return outcome
        seen.add(next_cursor)
        cursor = next_cursor


# --- cache ------------------------------------------------------------------


def _iso(wall: float) -> str:
    return datetime.datetime.fromtimestamp(wall, tz=datetime.timezone.utc).isoformat(timespec="seconds")


@dataclass(frozen=True)
class CacheEntry:
    raw: bytes
    content_type: str
    content_sha256: str
    stored_at: str
    age_seconds: float
    scope: str


class ResponseCache:
    """Explicit TTL, explicit scope, explicit refresh.

    The scope is part of the key. A response fetched with a credential is an answer to a
    different question than the same URL fetched without one — it may include records the
    uncredentialed caller is not entitled to see — so the two never share an entry in either
    direction. A `refresh=True` read bypasses and replaces rather than trusting the TTL.
    """

    def __init__(self, *, ttl: float = DEFAULT_CACHE_TTL, clock=time.monotonic, wall=time.time):
        self.ttl, self._clock, self._wall = ttl, clock, wall
        self._entries: dict[tuple[str, str], tuple[bytes, str, str, float, float]] = {}
        self.hits = self.stores = self.expired = self.refreshed = 0

    @staticmethod
    def key(url: str, params: dict | None) -> str:
        parts = [url] + [f"{name}={params[name]}" for name in sorted(params or {})]
        return hashlib.sha256("|".join(parts).encode()).hexdigest()

    def get(self, url: str, params: dict | None, scope: str, *, refresh: bool = False):
        if self.ttl <= 0:
            return None
        index = (self.key(url, params), scope)
        entry = self._entries.get(index)
        if entry is None:
            return None
        raw, content_type, digest, stored_wall, stored_mono = entry
        age = self._clock() - stored_mono
        if refresh or age > self.ttl:
            # A stale entry is dropped, not merely skipped: keeping it would let a later reader
            # believe the answer is still current. A forced refresh is counted apart from a TTL
            # expiry, because one is the caller's decision and the other is the clock's.
            del self._entries[index]
            if refresh:
                self.refreshed += 1
            else:
                self.expired += 1
            return None
        self.hits += 1
        return CacheEntry(raw=raw, content_type=content_type, content_sha256=digest,
                          stored_at=_iso(stored_wall), age_seconds=round(age, 3), scope=scope)

    def put(self, url: str, params: dict | None, scope: str, raw: bytes, content_type: str = "") -> str:
        digest = hashlib.sha256(raw).hexdigest()
        if self.ttl <= 0:
            return digest
        self._entries[(self.key(url, params), scope)] = (raw, content_type, digest,
                                                        self._wall(), self._clock())
        self.stores += 1
        return digest

    def summary(self) -> dict:
        return {"ttl_seconds": self.ttl, "entries": len(self._entries), "hits": self.hits,
                "stores": self.stores, "expired": self.expired, "refreshed": self.refreshed,
                "note": "缓存计数是整个会话累计的；requests_used 只是本次调用的"}


# --- governor ---------------------------------------------------------------


def retry_after_seconds(headers, *, wall=time.time) -> float | None:
    """Parse the provider's own instruction about when to come back.

    `Retry-After` may be a delay in seconds or an HTTP date; GitHub instead sends
    `x-ratelimit-reset` as a unix timestamp. All three are untrusted: a value that cannot be
    parsed yields `None` (the caller falls back to bounded backoff rather than to an immediate
    retry), and one that outlasts the cap is clamped.
    """
    lowered = {str(name).lower(): str(value) for name, value in (headers or {}).items()}
    raw = lowered.get("retry-after", "").strip()
    if raw:
        try:
            value: float | None = float(raw)
        except ValueError:
            try:
                moment = parsedate_to_datetime(raw)
            except (TypeError, ValueError):
                moment = None
            value = None if moment is None else (
                moment.timestamp() if moment.tzinfo else
                moment.replace(tzinfo=datetime.timezone.utc).timestamp()) - wall()
        if value is not None and value == value:  # the != test rejects NaN
            # A date already in the past means "proceed now", so it clamps to zero rather than
            # falling through to backoff: backing off there would wait when the provider said not to.
            return max(0.0, min(float(value), RETRY_AFTER_CAP))
    reset = lowered.get("x-ratelimit-reset", "").strip()
    if reset.lstrip("-").isdigit():
        delta = int(reset) - int(wall())
        if delta > 0:
            return min(float(delta), RETRY_AFTER_CAP)
    return None


class Governor:
    """One scheduler shared by every client in a call.

    Sharing is the point: a rate limit hit by the third request has to be honoured by the
    fourth, and a per-client budget cannot express that. The governor owns the total request
    count, a per-provider count, the provider's own deferral windows, bounded backoff, the
    response cache and cancellation.
    """

    def __init__(self, *, max_requests: int = 40, per_provider: dict | None = None,
                 seconds: float = 90.0, cache_ttl: float = DEFAULT_CACHE_TTL, cache=None,
                 sleep=time.sleep, clock=time.monotonic, wall=time.time):
        self.max_requests = max(0, int(max_requests))
        self.per_provider = dict(per_provider or {})
        self._sleep, self._clock, self._wall = sleep, clock, wall
        self.deadline = clock() + seconds
        # A caller may hand in a cache that outlives this governor, so one session asks the same
        # question once. It is deliberately not a module-level global: a cache shared across
        # sessions would hand one user's credentialed answer to another user's anonymous call.
        self.cache = cache or ResponseCache(ttl=cache_ttl, clock=clock, wall=wall)
        self._cancel = threading.Event()
        self._lock = threading.Lock()
        self.requests_used = 0
        self.by_provider: dict[str, int] = {}
        self.deferred: dict[str, float] = {}
        self.retries: dict[str, int] = {}
        self._windows: dict[str, float] = {}

    # -- scheduling ----------------------------------------------------------

    def _over_budget(self, provider: str) -> str:
        """Why this destination may not be asked again, or "" when it may. Caller holds the lock."""
        if self.requests_used >= self.max_requests:
            return (f"已达到共享请求预算 {self.max_requests} 次；本次没有继续请求。"
                    "这不表示来源没有更多内容，也不表示资源不存在")
        cap = self.per_provider.get(provider)
        if cap is not None and self.by_provider.get(provider, 0) >= cap:
            return f"{provider} 已达到单源请求预算 {cap} 次"
        return ""

    def ensure_available(self, provider: str) -> None:
        """Ask whether another request is affordable, without reserving one.

        Only the code that actually performs a request may call `acquire`. A paginator that
        reserved a slot per page on top of the client reserving one per request would count every
        page twice and report twice the traffic there was.
        """
        if self._cancel.is_set():
            raise Cancelled("调用已取消")
        with self._lock:
            reason = self._over_budget(provider)
        if reason:
            raise BudgetExhausted(reason)

    def acquire(self, provider: str) -> None:
        """Reserve one request, waiting out any window the provider asked for.

        The reservation is taken before the wait, so two callers cannot both spend the last
        slot while one of them sleeps.
        """
        if self._cancel.is_set():
            raise Cancelled("调用已取消")
        with self._lock:
            reason = self._over_budget(provider)
            if not reason:
                self.requests_used += 1
                self.by_provider[provider] = self.by_provider.get(provider, 0) + 1
            wait = self._windows.get(provider, 0.0) - self._clock()
            left = self.deadline - self._clock()
        if reason:
            raise BudgetExhausted(reason)
        if wait > 0 and wait > left:
            self._release(provider)
            raise BudgetExhausted(
                f"{provider} 要求等待约 {wait:.0f}s 才能继续，超出本次调用的时间预算；已停止而不是忽略该要求")
        if wait <= 0 and left <= 0:
            self._release(provider)
            raise BudgetExhausted("已达到本次调用的时间预算")
        if wait > 0:
            self._sleep(min(wait, RETRY_AFTER_CAP))
            if self._cancel.is_set():
                raise Cancelled("调用已取消")

    def _release(self, provider: str) -> None:
        """Give back a reservation that was never spent on a request.

        Counting a request that did not happen would make the coverage block report more
        network traffic than there was, which is the same kind of overclaim as reporting a
        page that was never read.
        """
        with self._lock:
            self.requests_used -= 1
            self.by_provider[provider] = max(0, self.by_provider.get(provider, 1) - 1)

    def defer(self, provider: str, headers=None) -> float:
        """Honour a provider's Retry-After / rate-limit reset, or back off with a bound."""
        seconds = retry_after_seconds(headers, wall=self._wall)
        source = "retry-after"
        if seconds is None:
            source = "backoff"
            with self._lock:
                seconds = min(BACKOFF_CAP, BACKOFF_BASE * (2 ** self.retries.get(provider, 0)))
        with self._lock:
            self._windows[provider] = max(self._windows.get(provider, 0.0), self._clock() + seconds)
            self.deferred[provider] = self.deferred.get(provider, 0.0) + seconds
        return seconds

    def note_retry(self, provider: str) -> bool:
        """Whether another attempt is allowed. Bounded, so retries cannot nest or loop."""
        with self._lock:
            attempts = self.retries.get(provider, 0) + 1
            self.retries[provider] = attempts
            return attempts < MAX_ATTEMPTS

    def note_success(self, provider: str) -> None:
        with self._lock:
            self.retries.pop(provider, None)

    def cancel(self) -> None:
        self._cancel.set()

    @property
    def cancelled(self) -> bool:
        return self._cancel.is_set()

    def window_remaining(self, provider: str) -> float:
        with self._lock:
            return max(0.0, self._windows.get(provider, 0.0) - self._clock())

    def summary(self) -> dict:
        with self._lock:
            return {"max_requests": self.max_requests, "requests_used": self.requests_used,
                    "per_provider_limits": dict(self.per_provider),
                    "per_provider_used": dict(self.by_provider),
                    "deferred_seconds": {name: round(value, 1)
                                         for name, value in self.deferred.items()},
                    "retries": dict(self.retries), "cancelled": self._cancel.is_set(),
                    "cache": self.cache.summary()}


# --- venue ------------------------------------------------------------------


@dataclass
class VenueFilter:
    """How a venue constraint was applied, and by whom.

    `strict` means the service itself filtered on a resolved stable source ID. `hint` means
    the words were passed through as a search hint, or applied after the fact — which must
    never be shown to a user as a strict filter, because it silently drops records whose
    venue string is spelled differently.
    """
    requested: str = ""
    mode: str = "none"          # none | strict | hint | unsupported | resolve_failed
    resolved_id: str = ""
    resolved_names: list = field(default_factory=list)
    detail: str = ""

    def as_dict(self) -> dict:
        return {"requested": self.requested, "mode": self.mode, "resolved_id": self.resolved_id,
                "resolved_names": list(self.resolved_names), "detail": self.detail}


# A venue family is often split across per-edition source records ("2022 IEEE/CVF CVPR",
# "2023 IEEE/CVF CVPR", …), so several IDs are OR-ed rather than trusting the first hit. The
# cap keeps the filter string bounded; the names that were used are always reported, because a
# strict filter that quietly matched one edition would look identical to one that matched all.
VENUE_MAX_SOURCES = 8


VENUE_STRICT_SOURCES = ("openalex",)
VENUE_HINT_SOURCES = ("semanticscholar",)
VENUE_MODE_LABELS = {
    "none": "未指定会议或期刊条件",
    "strict": "来源按解析出的稳定 source ID 严格过滤",
    "hint": "仅作为检索提示传入，不是严格过滤；拼写不同的同一会议可能漏掉",
    "unsupported": "该来源没有可用的会议过滤；条件未生效，结果不按会议收窄",
    "resolve_failed": "未能把会议名解析为稳定的 source ID；本次没有按会议过滤",
}
