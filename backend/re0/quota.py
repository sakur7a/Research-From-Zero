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
from urllib.parse import urlsplit

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
DESTINATION_BREAKERS_KEY = "destination_breakers"
PROBE_LEDGER_KEY = "model_probe_ledger"


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


def destination_key(base_url: str) -> str:
    """Canonical approved endpoint identity without userinfo, query, fragment or API key."""
    parsed = urlsplit(str(base_url).strip())
    host = (parsed.hostname or "").lower()
    if ":" in host:
        host = f"[{host}]"
    port = parsed.port
    if port and not ((parsed.scheme == "https" and port == 443) or
                     (parsed.scheme == "http" and port == 80)):
        host += f":{port}"
    path = "/" + "/".join(part for part in parsed.path.split("/") if part)
    return f"{parsed.scheme.lower()}://{host}{path.rstrip('/')}"


class DestinationBreakers:
    """Failure windows per approved model endpoint, never keyed by credential material."""

    def __init__(self, failure_threshold: int = BREAKER_FAILURE_THRESHOLD,
                 window_seconds: int = BREAKER_WINDOW_SECONDS,
                 cooldown_seconds: int = BREAKER_COOLDOWN_SECONDS):
        self.threshold, self.window, self.cooldown = failure_threshold, window_seconds, cooldown_seconds
        self._lock = threading.RLock()
        self._entries: dict[str, dict] = {}
        self._half_open_active: set[str] = set()

    def _entry(self, endpoint: str) -> dict:
        return self._entries.setdefault(endpoint, {"failures": 0, "first_failure_at": 0.0,
                                                   "opened_at": 0.0, "last_reason": "",
                                                   "updated_at": time.time()})

    def state(self, endpoint: str, now: float | None = None) -> str:
        moment = time.time() if now is None else now
        with self._lock:
            entry = self._entries.get(endpoint)
            if not entry or not entry.get("opened_at"):
                return "closed"
            return "half-open" if moment - entry["opened_at"] >= self.cooldown else "open"

    def wait_seconds(self, endpoint: str, now: float | None = None) -> float:
        moment = time.time() if now is None else now
        with self._lock:
            entry = self._entries.get(endpoint) or {}
            opened = float(entry.get("opened_at") or 0)
            return max(0.0, round(self.cooldown - (moment - opened), 1)) if opened else 0.0

    def refusal(self, endpoint: str, now: float | None = None) -> dict | None:
        moment = time.time() if now is None else now
        state = self.state(endpoint, moment)
        if state == "closed":
            return None
        with self._lock:
            entry = self._entries.get(endpoint) or {}
            if state == "half-open" and endpoint not in self._half_open_active:
                return None
            retry = 1 if state == "half-open" else max(
                1, int(self.cooldown - (moment - entry["opened_at"]) + 0.999))
            reason = str(entry.get("last_reason") or "目的地连续不可用")[:200]
            message = (f"模型目的地 {endpoint} 正在进行半开探测；已有一次探测在途，本次未发出请求。"
                       if state == "half-open" else
                       f"模型目的地 {endpoint} 的熔断已打开：连续 {int(entry.get('failures', 0))} 次不可用"
                       f"（{reason}），约 {retry} 秒后可进行一次探测；本次未发出请求。")
            return {"state": state, "retry_after": retry, "message": message}

    def acquire(self, endpoint: str, now: float | None = None) -> dict:
        moment = time.time() if now is None else now
        with self._lock:
            refusal = self.refusal(endpoint, moment)
            if refusal:
                return {"allowed": False, **refusal}
            state = self.state(endpoint, moment)
            if state == "half-open":
                self._half_open_active.add(endpoint)
            return {"allowed": True, "state": state, "retry_after": 0}

    def release(self, endpoint: str) -> None:
        with self._lock:
            self._half_open_active.discard(endpoint)

    def record_failure(self, endpoint: str, reason: str, now: float | None = None) -> dict:
        moment = time.time() if now is None else now
        with self._lock:
            entry = self._entry(endpoint)
            if entry["opened_at"] and moment - entry["opened_at"] < self.cooldown:
                entry["last_reason"] = (reason or "")[:200]
            else:
                if entry["failures"] and moment - entry["first_failure_at"] > self.window:
                    entry["failures"], entry["first_failure_at"] = 0, 0.0
                if not entry["failures"]:
                    entry["first_failure_at"] = moment
                entry["failures"] += 1
                entry["last_reason"] = (reason or "")[:200]
                if entry["failures"] >= self.threshold:
                    entry["opened_at"] = moment
            entry["updated_at"] = moment
            self._half_open_active.discard(endpoint)
            return dict(entry, state=self.state(endpoint, moment))

    def record_success(self, endpoint: str, now: float | None = None) -> dict:
        moment = time.time() if now is None else now
        with self._lock:
            entry = self._entry(endpoint)
            recovered = bool(entry["opened_at"])
            entry.update({"failures": 0, "first_failure_at": 0.0, "opened_at": 0.0,
                          "last_reason": "", "updated_at": moment})
            self._half_open_active.discard(endpoint)
            return dict(entry, state="closed", recovered=recovered)

    def snapshot(self) -> dict:
        with self._lock:
            active = sorted(self._entries.items(), key=lambda item: item[1].get("updated_at", 0),
                            reverse=True)[:100]
            return {"schema_version": 1,
                    "entries": {key: dict(value) for key, value in active}}

    def load(self, saved: dict) -> None:
        raw = saved.get("entries", {}) if isinstance(saved, dict) else {}
        if not isinstance(raw, dict):
            return
        moment = time.time()
        with self._lock:
            for endpoint, item in list(raw.items())[:100]:
                if (not isinstance(endpoint, str) or
                        not endpoint.startswith(("https://", "http://")) or not isinstance(item, dict)):
                    continue
                try:
                    failures = min(10_000, max(0, int(item.get("failures", 0))))
                    first = float(item.get("first_failure_at", 0))
                    opened = float(item.get("opened_at", 0))
                    updated = float(item.get("updated_at", 0))
                except (TypeError, ValueError):
                    continue
                if updated and moment - updated > self.window + self.cooldown and not opened:
                    continue
                self._entries[endpoint[:500]] = {
                    "failures": failures,
                    "first_failure_at": first if 0 <= first <= moment + 60 else 0.0,
                    "opened_at": opened if 0 <= opened <= moment + 60 else 0.0,
                    "last_reason": str(item.get("last_reason") or "")[:200],
                    "updated_at": updated if 0 <= updated <= moment + 60 else moment}


class ProbeLedger:
    """Per-owner bounded connection/model-list probes and credential-scoped cooldowns."""

    LIMITS = {"models": (6, 60), "connect": (3, 300)}

    def __init__(self, *, global_concurrency: int = 4):
        self.global_concurrency = global_concurrency
        self._lock = threading.RLock()
        self._history: dict[str, list[dict]] = {}
        self._cooldowns: dict[str, dict[str, dict]] = {}
        self._active: dict[str, dict] = {}

    def _prune(self, owner: str, moment: float) -> list[dict]:
        history = [item for item in self._history.get(owner, [])
                   if moment - item["at"] < max(window for _, window in self.LIMITS.values())]
        self._history[owner] = history
        self._cooldowns[owner] = {dest: item for dest, item in self._cooldowns.get(owner, {}).items()
                                  if item["until"] > moment}
        return history

    def reserve(self, owner: str, kind: str, endpoint: str, now: float | None = None) -> dict:
        moment = time.time() if now is None else now
        if kind not in self.LIMITS:
            raise ValueError("未知的探测类型")
        with self._lock:
            history = self._prune(owner, moment)
            if owner in self._active:
                return {"allowed": False, "status": 409, "retry_after": 1,
                        "message": "该账户已有模型探测在途；本次没有发出请求。"}
            if len(self._active) >= self.global_concurrency:
                return {"allowed": False, "status": 503, "retry_after": 2,
                        "message": "模型探测并发槽已满；本次没有发出请求。"}
            cooldown = self._cooldowns.get(owner, {}).get(endpoint)
            if cooldown:
                retry = max(1, int(cooldown["until"] - moment + 0.999))
                return {"allowed": False, "status": 429, "retry_after": retry,
                        "message": f"此账户对该模型服务的探测正冷却中（{cooldown['reason']}）；约 {retry} 秒后可重试。"}
            limit, window = self.LIMITS[kind]
            used = [item for item in history if item["kind"] == kind and moment - item["at"] < window]
            if len(used) >= limit:
                retry = max(1, int(window - (moment - used[0]["at"]) + 0.999))
                label = "模型列表" if kind == "models" else "连接测试"
                return {"allowed": False, "status": 429, "retry_after": retry,
                        "message": f"此账户的{label}预算已用尽：{limit} 次/{window} 秒；"
                                   f"本次没有发出请求，约 {retry} 秒后可重试。"}
            record = {"at": moment, "kind": kind, "destination": endpoint, "status": "reserved"}
            history.append(record)
            self._active[owner] = record
            return {"allowed": True, "record": record, "remaining": limit - len(used) - 1}

    def finish(self, owner: str, *, status: str, retry_after: float = 0,
               reason: str = "", now: float | None = None) -> None:
        moment = time.time() if now is None else now
        with self._lock:
            record = self._active.pop(owner, None)
            if not record:
                return
            record["status"] = status[:40]
            if retry_after > 0:
                self._cooldowns.setdefault(owner, {})[record["destination"]] = {
                    "until": moment + min(3600, max(1, retry_after)),
                    "reason": (reason or "HTTP 429")[:120]}

    def cooldown(self, owner: str, endpoint: str, retry_after: float,
                 reason: str = "HTTP 429", now: float | None = None) -> None:
        moment = time.time() if now is None else now
        if retry_after <= 0:
            return
        with self._lock:
            self._cooldowns.setdefault(owner, {})[endpoint] = {
                "until": moment + min(3600, max(1, retry_after)),
                "reason": (reason or "HTTP 429")[:120]}

    def refusal(self, owner: str, endpoint: str, now: float | None = None) -> dict | None:
        moment = time.time() if now is None else now
        with self._lock:
            self._prune(owner, moment)
            item = self._cooldowns.get(owner, {}).get(endpoint)
            if not item:
                return None
            retry = max(1, int(item["until"] - moment + 0.999))
            return {"status": 429, "retry_after": retry,
                    "message": f"此账户对该模型服务的请求处于冷却中（{item['reason']}）；约 {retry} 秒后可重试。"}

    def describe(self, owner: str, now: float | None = None) -> dict:
        moment = time.time() if now is None else now
        with self._lock:
            history = self._prune(owner, moment)
            counts = {kind: sum(1 for item in history
                                if item["kind"] == kind and moment - item["at"] < window)
                      for kind, (_, window) in self.LIMITS.items()}
            limits = {kind: {"limit": limit, "window_seconds": window}
                      for kind, (limit, window) in self.LIMITS.items()}
            cooldowns = [{"destination": key,
                          "retry_after": max(0, int(value["until"] - moment + 0.999)),
                          "reason": value["reason"]}
                         for key, value in self._cooldowns.get(owner, {}).items()]
            return {"counts": counts, "limits": limits, "active": owner in self._active,
                    "cooldowns": cooldowns,
                    "recent": [{key: item[key] for key in ("at", "kind", "destination", "status")}
                               for item in history[-10:]]}

    def snapshot(self) -> dict:
        with self._lock:
            history = {owner: [dict(item) for item in rows[-20:]]
                       for owner, rows in sorted(self._history.items(),
                                                key=lambda pair: pair[1][-1]["at"] if pair[1] else 0,
                                                reverse=True)[:500]}
            cooldowns = {owner: {destination: dict(value) for destination, value in items.items()}
                         for owner, items in list(self._cooldowns.items())[:500]}
            return {"schema_version": 1, "history": history, "cooldowns": cooldowns}

    def load(self, saved: dict) -> None:
        if not isinstance(saved, dict):
            return
        moment = time.time()
        with self._lock:
            for owner, rows in list((saved.get("history") or {}).items())[:500]:
                if not isinstance(owner, str) or not isinstance(rows, list):
                    continue
                clean = []
                for item in rows[-20:]:
                    try:
                        at = float(item.get("at", 0))
                    except (TypeError, ValueError):
                        continue
                    kind, destination = item.get("kind"), item.get("destination")
                    if kind not in self.LIMITS or not isinstance(destination, str):
                        continue
                    status = str(item.get("status") or "interrupted")[:40]
                    if status == "reserved":
                        status = "interrupted"
                    if 0 <= moment - at < 300:
                        clean.append({"at": at, "kind": kind, "destination": destination[:500],
                                      "status": status})
                if clean:
                    self._history[owner[:200]] = clean
            for owner, items in list((saved.get("cooldowns") or {}).items())[:500]:
                if not isinstance(owner, str) or not isinstance(items, dict):
                    continue
                clean = {}
                for endpoint, value in list(items.items())[:20]:
                    if not isinstance(endpoint, str) or not isinstance(value, dict):
                        continue
                    try:
                        until = float(value.get("until", 0))
                    except (TypeError, ValueError):
                        continue
                    if moment < until <= moment + 3600:
                        clean[endpoint[:500]] = {"until": until,
                                                 "reason": str(value.get("reason") or "HTTP 429")[:120]}
                if clean:
                    self._cooldowns[owner[:200]] = clean


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


def site_emergency_stop_from_env(os_environ) -> bool:
    """Explicit deployer stop, separate from any provider or credential failure."""
    raw = (os_environ.get("RE0_SITE_EMERGENCY_STOP") or "").strip().lower()
    if raw not in {"", "0", "1", "false", "true", "no", "yes", "off", "on"}:
        raise ValueError("RE0_SITE_EMERGENCY_STOP 只能是 0/1 或 false/true")
    return raw in {"1", "true", "yes", "on"}


class Quota:
    """The ceilings a request can hit, in the order the service consults them.

    Built once in `create_app` and shared by the HTTP middleware and the agent runtime, because two
    copies would mean two answers to "may this run start" and only one of them would be the truth.
    """

    def __init__(self, *, requests_per_minute: int = 0, site_requests_per_minute: int = 0,
                 tasks_per_hour: int = 0, breaker: CircuitBreaker | None = None,
                 emergency_stop: bool = False,
                 destination_breakers: DestinationBreakers | None = None,
                 probes: ProbeLedger | None = None):
        self.limits = {"RE0_REQUESTS_PER_MINUTE": requests_per_minute,
                       "RE0_SITE_REQUESTS_PER_MINUTE": site_requests_per_minute,
                       "RE0_TASKS_PER_HOUR": tasks_per_hour}
        self.owner_requests = RateLimiter(requests_per_minute, 60)
        self.site_requests = RateLimiter(site_requests_per_minute, 60)
        self.owner_tasks = RateLimiter(tasks_per_hour, 3600)
        # Kept only for direct CircuitBreaker users during the transition; runtime admission uses
        # this explicit deployer-controlled stop and destination_breakers below.
        self.breaker = breaker or CircuitBreaker()
        self.emergency_stop = emergency_stop
        self.destination_breakers = destination_breakers or DestinationBreakers()
        self.probes = probes or ProbeLedger()

    @classmethod
    def from_env(cls, environ=None, *, hosted: bool) -> "Quota":
        values = os.environ if environ is None else environ
        limits = quota_limits_from_env(values, hosted=hosted)
        return cls(requests_per_minute=limits["RE0_REQUESTS_PER_MINUTE"],
                   site_requests_per_minute=limits["RE0_SITE_REQUESTS_PER_MINUTE"],
                   tasks_per_hour=limits["RE0_TASKS_PER_HOUR"],
                   emergency_stop=site_emergency_stop_from_env(values))

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
        which is the fact an operator needs and which a single site-wide counter would hide. Logged-out
        traffic shares one bucket — it has no owner to key on — and the refusal says so rather than
        telling somebody about an account they do not have yet.
        """
        for limiter, scope, key, subject in (
                (self.owner_requests, "请求", owner, "该账户" if owner else "未登录"),
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
            "probe_mode": "bounded",
            "requests_used": {"account": self.owner_requests.used(owner),
                              "site": self.site_requests.used(SITE_KEY)},
            "tasks_used_last_hour": self.owner_tasks.used(owner),
            "breaker": {"state": "open" if self.emergency_stop else "closed",
                        "scope": "site_emergency_stop", "operator_controlled": True,
                        "note": "全站急停只由部署者通过 RE0_SITE_EMERGENCY_STOP 控制；模型目的地故障"
                                "单独计入 destination_breaker，用户凭据错误不影响其他账户"},
            "probes": self.probes.describe(owner),
        }
