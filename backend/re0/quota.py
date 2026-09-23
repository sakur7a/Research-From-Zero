"""Request quotas and a site-wide circuit breaker: how much anyone may spend, and when to stop.

Isolation (#13's first half) answers *who may see what*. These answer *how much may be spent*, which
is the question a reachable service cannot leave open: one careless loop, or one hostile session, is
otherwise an unbounded bill and an unbounded load on the scholarly APIs this tool shares with
everybody else.

Three rules shape the implementation:

* **A limit is keyed on something the caller cannot forge.** In hosted mode that is the verified
  identity's owner; a header a visitor controls (`X-Forwarded-For`, a cookie they set) must not mint
  a fresh bucket. In local mode there is one reader at one keyboard, so per-owner limits would only
  punish the owner — they are off by default there, and the breaker still applies.
* **Refusing is not the same as having done nothing.** Every refusal says what was refused, what the
  limit is, and when it resets, and it never spends a *call* budget: a request turned away at the
  door did not reach a model, and no charged work was created. It does occupy the arrival window it
  was refused in, because it did arrive.
* **The breaker protects the process, not the truth.** Opening it stops *new* work; it never kills a
  turn in flight, because killing one could discard a provider call that will still be billed. An
  unknown in-flight cost stays unknown, and is reported as such rather than as zero.

Which ceilings exist, mapped to the words the issue uses: *per account* is the request window and the
task window below; *per session* is the cumulative model/tool-call ledger in `agent/runtime.py`
(`session_caps`, enforced every turn); *site-wide* is the request window keyed on the whole service
plus this breaker, on top of the single execution slot that already serialises paid work.
"""
from __future__ import annotations

import os
import threading
import time

# Defaults, per owner, in hosted mode. Chosen to be far above honest use and far below a loop:
# a human clicking through the workbench does not make 120 API requests in a minute, and a turn of
# research with follow-ups does not need twelve task starts an hour.
DEFAULT_REQUESTS_PER_MINUTE = 120
DEFAULT_SITE_REQUESTS_PER_MINUTE = 600
DEFAULT_TASKS_PER_HOUR = 12
# Eight consecutive failures inside two minutes says the destination or the network is down, not
# that one query was unlucky. Ninety seconds of cooldown is long enough for a rate limit to reset
# and short enough that an operator who fixed something is not waiting all afternoon.
BREAKER_FAILURE_THRESHOLD = 8
BREAKER_WINDOW_SECONDS = 120
BREAKER_COOLDOWN_SECONDS = 90

# The one key the site-wide window is counted under, and the owner the breaker's persisted state is
# stored for. Both start with the reserved `local` prefix, so no account workspace (`ws_…`) and no
# account name can collide with them.
SITE_KEY = "local:site"
SITE_SETTINGS_OWNER = SITE_KEY
BREAKER_SETTINGS_KEY = "circuit_breaker"


class RateLimiter:
    """A sliding-window counter per key, with the window's own bookkeeping exposed.

    Sliding rather than fixed-interval: a fixed bucket refills all at once, so a caller can spend two
    buckets back to back at the boundary — the classic "I waited one second and the limit was gone"
    bypass. A window that expires entry by entry has no such cliff.
    """

    def __init__(self, limit: int, window_seconds: int):
        self.limit, self.window = limit, window_seconds
        self._hits: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    def _live(self, hits: list[float], moment: float) -> list[float]:
        return [item for item in hits if moment - item < self.window]

    def check(self, key: str, now: float | None = None) -> dict:
        """Record one attempt and report whether it was allowed, without raising.

        Returns the facts a refusal needs: `allowed`, `remaining`, `retry_after` and the window, so
        the caller can answer with a number instead of a shrug.
        """
        if self.limit <= 0:
            return {"allowed": True, "remaining": None, "retry_after": 0, "limit": self.limit,
                    "window_seconds": self.window}
        moment = time.monotonic() if now is None else now
        with self._lock:
            hits = self._live(self._hits.get(key, []), moment)
            allowed = len(hits) < self.limit
            if allowed:
                hits.append(moment)
            self._hits[key] = hits
            retry_after = 0.0 if allowed else max(0.0, self.window - (moment - hits[0]))
            return {"allowed": allowed, "remaining": max(0, self.limit - len(hits)),
                    "retry_after": round(retry_after, 1), "limit": self.limit,
                    "window_seconds": self.window}

    def used(self, key: str, now: float | None = None) -> int:
        """How much of the window this key has spent, without spending any of it."""
        if self.limit <= 0:
            return 0
        moment = time.monotonic() if now is None else now
        with self._lock:
            return len(self._live(self._hits.get(key, []), moment))

    def prune(self, now: float | None = None) -> None:
        """Drop keys whose window has fully passed, so a long-lived process does not remember
        everybody who ever knocked."""
        moment = time.monotonic() if now is None else now
        with self._lock:
            for key in [key for key, hits in self._hits.items()
                        if not hits or moment - hits[-1] >= self.window]:
                del self._hits[key]


class CircuitBreaker:
    """Opens after a run of consecutive destination failures; a successful probe closes it.

    Consecutive, not total: an intermittent 429 from one source is normal scholarly-API weather and
    must not stop unrelated work, while eight failures in a row with nothing succeeding in between
    says the model service is unreachable and every further attempt is a cost with no chance of a
    result. Only failures of *the destination* are fed to it (see `ModelError.destination`): one
    account's wrong key or empty balance is that account's problem, and counting it here would let a
    stranger with a bad credential shut the service down for everybody.

    The state is a plain dict so it can be persisted beside the rest of the service's settings: a
    restart must not be a way to walk around an open breaker.
    """

    def __init__(self, failure_threshold: int = BREAKER_FAILURE_THRESHOLD,
                 window_seconds: int = BREAKER_WINDOW_SECONDS,
                 cooldown_seconds: int = BREAKER_COOLDOWN_SECONDS):
        self.threshold, self.window, self.cooldown = failure_threshold, window_seconds, cooldown_seconds
        # Re-entrant: `refusal()` and `snapshot()` answer with `state()`, which locks too. A plain
        # Lock here deadlocks the first request that asks whether it may proceed.
        self._lock = threading.RLock()
        self._state = {"failures": 0, "first_failure_at": 0.0, "opened_at": 0.0,
                       "last_reason": "", "closed_by": ""}

    def load(self, saved: dict) -> None:
        """Adopt a persisted state.

        Timestamps are monotonic offsets, so they are only comparable inside one process: a breaker
        that opened before this process started is still open, but its cooldown is re-measured from
        now, because the outage's clock and this one are not the same clock. Junk from a hand-edited
        row is dropped rather than trusted — a stored `"abc"` in a numeric field would otherwise
        raise on the next request.
        """
        with self._lock:
            for key in self._state:
                if key not in saved:
                    continue
                raw = saved[key]
                if key.endswith("_at") or key == "failures":
                    try:
                        self._state[key] = float(raw)
                    except (TypeError, ValueError):
                        continue
                else:
                    self._state[key] = str(raw)[:200]
            if self._state["opened_at"]:
                self._state["opened_at"] = time.monotonic()

    def snapshot(self) -> dict:
        with self._lock:
            return dict(self._state, state=self.state())

    def state(self, now: float | None = None) -> str:
        moment = time.monotonic() if now is None else now
        with self._lock:
            if not self._state["opened_at"]:
                return "closed"
            if moment - self._state["opened_at"] >= self.cooldown:
                return "half-open"
            return "open"

    def wait_seconds(self, now: float | None = None) -> float:
        """How long until the breaker admits a probe again; 0 when it is not holding anything."""
        moment = time.monotonic() if now is None else now
        with self._lock:
            if not self._state["opened_at"]:
                return 0.0
            return max(0.0, round(self.cooldown - (moment - self._state["opened_at"]), 1))

    def refusal(self, now: float | None = None) -> str | None:
        """Why new work must wait, or None when the breaker lets it through."""
        with self._lock:
            state = self.state(now)
            if state == "closed":
                return None
            if state == "half-open":
                return None  # one probe is exactly what half-open means; the probe decides.
            return (f"全站熔断已打开：模型服务连续 {int(self._state['failures'])} 次不可用"
                    f"（{self._state['last_reason'] or '未记录原因'}）。约 {self.wait_seconds(now)} 秒后"
                    "进入半开状态，届时一次成功的连接测试会闭合熔断。本次请求没有创建任务，也没有产生"
                    "任何花费；正在执行的任务不会被中断，其已发生或在途的花费照实保留，不会被记成零。")

    def record_failure(self, reason: str, now: float | None = None) -> dict:
        moment = time.monotonic() if now is None else now
        with self._lock:
            if self._state["opened_at"]:
                if moment - self._state["opened_at"] >= self.cooldown:
                    # A probe during half-open failed: re-arm the cooldown from this attempt. Left at
                    # its old timestamp the breaker would sit permanently half-open, admitting one
                    # paid attempt at every moment — the exact load it exists to stop.
                    self._state["opened_at"] = moment
                self._state["last_reason"] = (reason or "")[:200]
                return dict(self._state, state=self.state(moment))
            if self._state["failures"] and moment - self._state["first_failure_at"] > self.window:
                # A failure after a quiet window starts a new run; the old one described weather
                # that has since passed.
                self._state["failures"], self._state["first_failure_at"] = 0, moment
            if not self._state["failures"]:
                self._state["first_failure_at"] = moment
            self._state["failures"] += 1
            self._state["last_reason"] = (reason or "")[:200]
            if self._state["failures"] >= self.threshold:
                self._state["opened_at"] = moment
                self._state["closed_by"] = ""
            return dict(self._state, state=self.state(moment))

    def record_success(self, now: float | None = None) -> dict:
        moment = time.monotonic() if now is None else now
        with self._lock:
            was = self.state(moment)
            if was != "closed":
                self._state["closed_by"] = "probe" if was == "half-open" else "success"
            self._state.update({"failures": 0, "first_failure_at": 0.0, "opened_at": 0.0})
            return dict(self._state, state="closed")


def quota_limits_from_env(os_environ, *, hosted: bool) -> dict:
    """Read the three knobs, refusing nonsense rather than silently adopting it.

    Unset means the documented default in hosted mode and *off* in local mode: a ceiling that only
    ever throttles the one person at the keyboard is noise, and `re0 paper search` on a laptop should
    not fail because of a number nobody chose. A negative value raises — taken literally it would mean
    "allow nothing" and if clamped "allow everything", and neither is what someone typing `-1` meant.
    Zero means off, and says so.
    """
    out = {}
    for name, default in (("RE0_REQUESTS_PER_MINUTE", DEFAULT_REQUESTS_PER_MINUTE),
                          ("RE0_SITE_REQUESTS_PER_MINUTE", DEFAULT_SITE_REQUESTS_PER_MINUTE),
                          ("RE0_TASKS_PER_HOUR", DEFAULT_TASKS_PER_HOUR)):
        raw = (os_environ.get(name) or "").strip()
        if not raw:
            out[name] = default if hosted else 0
            continue
        try:
            value = int(raw)
        except ValueError as exc:
            raise ValueError(f"{name} 必须是整数（0 表示关闭），收到 {raw!r}") from exc
        if value < 0:
            raise ValueError(f"{name} 不能为负（0 表示关闭），收到 {value}")
        out[name] = value
    return out


class Quota:
    """The ceilings a request can hit, in the order the service consults them.

    Built once in `create_app` and shared by the HTTP middleware and the agent runtime, because two
    copies would mean two answers to "may this run start" and only one of them would be the truth.
    """

    def __init__(self, *, requests_per_minute: int = 0, site_requests_per_minute: int = 0,
                 tasks_per_hour: int = 0, breaker: CircuitBreaker | None = None):
        self.limits = {"RE0_REQUESTS_PER_MINUTE": requests_per_minute,
                       "RE0_SITE_REQUESTS_PER_MINUTE": site_requests_per_minute,
                       "RE0_TASKS_PER_HOUR": tasks_per_hour}
        self.owner_requests = RateLimiter(requests_per_minute, 60)
        self.site_requests = RateLimiter(site_requests_per_minute, 60)
        self.owner_tasks = RateLimiter(tasks_per_hour, 3600)
        self.breaker = breaker or CircuitBreaker()

    @classmethod
    def from_env(cls, environ=None, *, hosted: bool) -> "Quota":
        limits = quota_limits_from_env(os.environ if environ is None else environ, hosted=hosted)
        return cls(requests_per_minute=limits["RE0_REQUESTS_PER_MINUTE"],
                   site_requests_per_minute=limits["RE0_SITE_REQUESTS_PER_MINUTE"],
                   tasks_per_hour=limits["RE0_TASKS_PER_HOUR"])

    @property
    def enabled(self) -> bool:
        return any(value > 0 for value in self.limits.values())

    @property
    def mode(self) -> str:
        """One word an operator can put in a status page: is anything actually being counted."""
        return "enforced" if self.enabled else "off"

    def _gate(self, limiter: RateLimiter, key: str, scope: str, subject: str) -> dict:
        verdict = limiter.check(key)
        if verdict["allowed"]:
            return verdict
        verdict["message"] = (f"{subject}{scope}配额已用尽：{verdict['limit']} 次/"
                              f"{verdict['window_seconds']} 秒。本次请求没有执行，没有创建任务，"
                              f"也没有产生模型调用或费用；约 {verdict['retry_after']} 秒后可再试。")
        return dict(verdict, scope=scope, allowed=False)

    def check_request(self, owner: str) -> dict:
        """One API request arriving from *owner*: their own window, then the site's.

        The account window is consulted first so a flooded service can say *who* is being unfair,
        which is the fact an operator needs and which a single site-wide counter would hide.
        """
        for limiter, scope, key, subject in (
                (self.owner_requests, "请求", owner, "该账户"),
                (self.site_requests, "请求", SITE_KEY, "全站")):
            verdict = self._gate(limiter, key, scope, subject)
            if not verdict["allowed"]:
                return verdict
        return {"allowed": True, "remaining": None, "retry_after": 0, "limit": 0,
                "window_seconds": 0, "scope": ""}

    def check_task(self, owner: str) -> dict:
        """One turn of paid work starting for *owner*."""
        verdict = self._gate(self.owner_tasks, owner, "任务", "该账户")
        if verdict["allowed"]:
            verdict["scope"] = "任务"
        return verdict

    def describe(self, owner: str) -> dict:
        """What this caller may know about the ceilings: their own usage, and the site's state.

        Never another owner's remaining budget — `queue` already withholds whose slot it is for the
        same reason.
        """
        return {
            "limits": dict(self.limits),
            "mode": "enforced" if self.enabled else "off",
            "requests_used": {"account": self.owner_requests.used(owner),
                              "site": self.site_requests.used(SITE_KEY)},
            "tasks_used_last_hour": self.owner_tasks.used(owner),
            "breaker": {"state": self.breaker.state(),
                        "consecutive_failures": int(self.breaker.snapshot()["failures"]),
                        "threshold": self.breaker.threshold,
                        "cooldown_seconds": self.breaker.cooldown,
                        "note": "只有模型服务连续不可用会计入全站熔断；某个账户的 Key 无效或余额不足"
                                "不会替别人关掉服务"},
        }
