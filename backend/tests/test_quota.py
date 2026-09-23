"""How much anyone may spend, and when the service stops answering: quotas, windows, breaker (#13).

The isolation tests in `test_auth.py` ask who may see what. These ask the other question a reachable
service has to answer — how much may be spent, by whom, and what happens when the model service is
down. Everything here is offline: every model call goes to a stub, and no provider is contacted.
"""
import threading
import time

import httpx
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from re0.agent.model import ModelError
from re0.agent.schemas import FollowUpInput, RetryInput
from re0.deployment import LOCAL_OWNER
from re0.main import create_app
from re0.quota import (BREAKER_FAILURE_THRESHOLD, CircuitBreaker, Quota,
                       quota_limits_from_env)

HOSTED = {"RE0_MODE": "hosted", "RE0_SESSION_SECRET": "a-hosted-secret-that-is-long-enough-to-use",
          "RE0_PUBLIC_ENTRY": "https://re0.example.org",
          "RE0_ALLOWED_ORIGINS": "https://re0.example.org",
          "RE0_STORAGE_MODE": "ephemeral-demo"}
WRITE = {"X-Re0-Client": "web", "Content-Type": "application/json"}
CONFIG = {"base_url": "https://api.openai.com/v1", "model": "fixture-model",
          "api_key": "sk-test-do-not-persist", "trust_endpoint": True}
GOAL = {"goal": "查找 layout 论文及资源，保留证据", "consent_to_send": True}
PASSWORD = "a-password-nobody-guesses"


def _refuse(request):
    raise AssertionError(f"Unexpected network request: {request.url}")


def generous(**overrides) -> Quota:
    """A ceiling large enough not to interfere, except where the test names one."""
    limits = {"requests_per_minute": 500, "site_requests_per_minute": 500, "tasks_per_hour": 500}
    limits.update(overrides)
    return Quota(**limits)


def local_app(tmp_path, *, quota=None, model=None, name="quota"):
    return create_app(str(tmp_path / f"{name}.sqlite3"), httpx.MockTransport(_refuse), quota=quota,
                      model_factory=(lambda config: model) if model is not None else None)


def hosted_app(tmp_path, *, quota=None, name="hosted"):
    from re0.deployment import from_env
    return create_app(str(tmp_path / f"{name}.sqlite3"), httpx.MockTransport(_refuse),
                      deployment=from_env(HOSTED).require_startable(), quota=quota)


def browser(app, username):
    """One logged-in account with its own cookie jar, as in the isolation tests."""
    app.state.accounts.create_account(username, PASSWORD)
    client = TestClient(app, headers=WRITE, base_url="https://testserver")
    assert client.post("/api/auth/login",
                       json={"username": username, "password": PASSWORD}).status_code == 200
    return client


class Stub:
    """A model that reports what it is told to report, and is never reached over the network.

    `hold` keeps the single execution slot occupied until the test lets it go, so "the slot is held"
    is a fact the test controls rather than one it races; `entered` says the worker got that far.
    """

    def __init__(self, outcome="down"):
        self.outcome = outcome
        self.calls = 0
        self.entered = threading.Event()
        self.let_go = threading.Event()

    def _report(self):
        self.calls += 1
        if self.outcome == "hold":
            self.entered.set()
            if not self.let_go.wait(20):
                return ModelError("fixture：测试没有释放这一轮", destination=False)
            return ModelError("fixture：本轮到此结束", destination=False)
        if self.outcome == "down":
            return ModelError("模型连接或读取失败；检查网络和模型服务地址", destination=True)
        if self.outcome == "denied":
            return ModelError("模型接口返回 HTTP 401；请检查权限、余额、模型名", destination=False)
        return None

    def complete(self, messages, tools, timeout=None):
        failure = self._report()
        if failure:
            raise failure
        return {"message": {"role": "assistant", "content": "", "tool_calls": []},
                "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
                "usage_reported": True}

    def test(self):
        failure = self._report()
        if failure:
            raise failure
        return {"ok": True, "tool_calling": True, "usage": {}, "note": "fixture"}


def release(app, deadline=20):
    """Wait for the execution slot to empty; the worker clears it asynchronously."""
    end = time.monotonic() + deadline
    while time.monotonic() < end:
        if not app.state.agent.busy():
            return True
        time.sleep(0.02)
    raise AssertionError("the execution slot never came free")


def open_breaker(app, times=BREAKER_FAILURE_THRESHOLD):
    for index in range(times):
        app.state.agent._record_breaker(failure=f"fixture：模型服务不可用 {index}")
    return app.state.agent.quota.breaker.state()


def breaker_state(client):
    return client.get("/api/health").json()["quota"]["breaker"]


# ------------------------------------------------------------------------------- the request window

def test_a_window_slides_instead_of_refilling_all_at_once():
    """The classic bypass is a fixed bucket: spend two of them either side of the boundary.

    A window that ages out entry by entry has no boundary to stand on, so four requests inside two
    seconds stay impossible even though each of them falls in "a different minute".
    """
    limiter = generous(requests_per_minute=2).owner_requests
    admitted = [moment for moment in (float(second) for second in range(0, 90))
                if limiter.check("ws_a", now=moment)["allowed"]]
    assert admitted == [0.0, 1.0, 60.0, 61.0], admitted
    # The second burst waits for the first request to age out — 60 s after it, not 0 s after a
    # calendar boundary. A fixed bucket refills all at once, so 59, 59.9, 60 and 60.1 all fit.
    refused = limiter.check("ws_a", now=70)
    assert not refused["allowed"] and refused["retry_after"] == 50.0
    assert limiter.used("ws_a", now=70) == 2
    assert limiter.check("ws_other", now=70)["allowed"], "one caller's window is not another's"


def test_used_reports_the_window_without_spending_it():
    limiter = generous(requests_per_minute=3).owner_requests
    assert limiter.used("ws_b") == 0
    limiter.check("ws_b", now=0)
    assert limiter.used("ws_b", now=1) == 1
    assert limiter.used("ws_b", now=1) == 1, "asking is not spending"
    assert limiter.used("ws_other", now=1) == 0


def test_zero_means_off_and_counts_nothing():
    limiter = Quota()
    assert [limiter.check_request(LOCAL_OWNER)["allowed"] for _ in range(1000)] == [True] * 1000
    assert [limiter.check_task(LOCAL_OWNER)["allowed"] for _ in range(1000)] == [True] * 1000
    assert limiter.mode == "off" and not limiter.enabled
    assert limiter.owner_requests.used(LOCAL_OWNER) == 0


def test_prune_forgets_everybody_who_ever_knocked():
    limiter = generous(requests_per_minute=2).owner_requests
    limiter.check("ws_c", now=0)
    limiter.check("ws_d", now=30)
    assert len(limiter._hits) == 2
    limiter.prune(now=200)
    assert limiter._hits == {}


def test_one_accounts_flood_does_not_stop_another_accounts_readers(tmp_path):
    """Per-account first: the operator needs to know who is being unfair, not only that it is busy."""
    app = hosted_app(tmp_path, quota=generous(requests_per_minute=3))
    a, b = browser(app, "alice"), browser(app, "bob")
    for _ in range(3):
        assert a.get("/api/papers").status_code == 200
    flooded = a.get("/api/papers")
    assert flooded.status_code == 429
    assert "该账户请求配额已用尽" in flooded.text
    assert "没有产生模型调用或费用" in flooded.text
    assert flooded.headers["retry-after"]
    # Bob did nothing, so Bob is not punished for it.
    assert b.get("/api/papers").status_code == 200


def test_a_forged_source_address_cannot_mint_a_fresh_bucket(tmp_path):
    """The key is the verified identity, so everything a caller controls must be inert here."""
    app = hosted_app(tmp_path, quota=generous(requests_per_minute=4), name="forge")
    anonymous = TestClient(app, base_url="https://testserver")
    for index in range(4):
        response = anonymous.get("/api/health", headers={"X-Forwarded-For": f"203.0.113.{index}",
                                                         "X-Real-IP": f"198.51.100.{index}"})
        assert response.status_code == 200, response.text
    for index in range(4):
        assert anonymous.get("/api/health",
                             headers={"X-Forwarded-For": f"203.0.113.{100 + index}"}).status_code == 429
    # Logged-out traffic shares one bucket rather than getting a new one per invented address, and it
    # is refused before the 401: hitting a ceiling is the news, whatever the route would have said.
    assert anonymous.get("/api/papers").status_code == 429


def test_a_refused_write_still_occupies_the_window_it_arrived_in(tmp_path):
    """Counting only accepted requests would leave the cheap rejection path unlimited."""
    app = hosted_app(tmp_path, quota=generous(requests_per_minute=4), name="refused")
    carol = browser(app, "carol")
    for _ in range(4):
        assert carol.post("/api/papers", json={"title": "x"},
                          headers={"X-Re0-Client": "curl"}).status_code == 403
    assert carol.get("/api/papers").status_code == 429


def test_a_site_wide_ceiling_says_so_and_blames_the_site_not_the_caller(tmp_path):
    """Total load is a different fact from one caller's load, and gets its own answer."""
    # Six in the window, two of them spent on the logins that set this up.
    app = hosted_app(tmp_path, quota=generous(site_requests_per_minute=6), name="site")
    a, b = browser(app, "dave"), browser(app, "erin")
    for _ in range(3):
        assert a.get("/api/health").status_code == 200
    assert b.get("/api/health").status_code == 200
    refused = b.get("/api/health")
    assert refused.status_code == 429 and "全站请求配额已用尽" in refused.text
    assert "该账户" not in refused.text


def test_the_site_ceiling_is_one_bucket_however_many_accounts_push(tmp_path):
    """Site-wide means the sum, so the same flood from two accounts must not double it."""
    app = hosted_app(tmp_path, quota=generous(site_requests_per_minute=10), name="sum")
    a, b = browser(app, "frank"), browser(app, "grace")
    codes = [(a if index % 2 else b).get("/api/health").status_code for index in range(12)]
    assert codes.count(200) == 8, codes          # 2 logins + 8 = the ten the site allows
    assert codes.count(429) == 4, codes


# -------------------------------------------------------------------------------- the task window

def test_a_turn_that_never_started_did_not_cost_one_of_your_starts(tmp_path):
    """Busy first, breaker second, window last — and only a launch spends it.

    Otherwise a client that retries while the single slot is held would burn its hourly allowance on
    requests that were refused before doing anything, which is the opposite of what a ceiling is for.
    """
    held = Stub("hold")
    app = local_app(tmp_path, quota=generous(tasks_per_hour=1), model=held)
    with TestClient(app, headers=WRITE) as client:
        client.put("/api/agent/config", json=CONFIG)
        assert client.post("/api/agent/runs", json=GOAL).status_code == 202
        assert held.entered.wait(20)
        busy = client.post("/api/agent/runs", json=GOAL)
        assert busy.status_code == 409, busy.text
        assert "没有产生任何花费" in busy.text
        assert app.state.agent.quota.owner_tasks.used(LOCAL_OWNER) == 1, \
            "a busy refusal spent the hourly allowance"
        held.let_go.set()
        release(app)
        spent = client.post("/api/agent/runs", json=GOAL)
        assert spent.status_code == 429, spent.text
        assert "任务配额已用尽" in spent.text and spent.headers["retry-after"]
        assert app.state.agent.quota.owner_tasks.used(LOCAL_OWNER) == 1


# ----------------------------------------------------------------------------------- the breaker

def test_eight_consecutive_failures_of_the_destination_open_it():
    breaker = CircuitBreaker()
    for _ in range(BREAKER_FAILURE_THRESHOLD - 1):
        breaker.record_failure("模型连接或读取失败")
    assert breaker.state() == "closed" and breaker.refusal() is None
    breaker.record_failure("模型连接或读取失败")
    refusal = breaker.refusal()
    assert breaker.state() == "open"
    assert "全站熔断已打开" in refusal and "连续 8 次不可用" in refusal
    # The two things a reader must not be left guessing: nothing was bought, and nothing already
    # running is being thrown away.
    assert "没有产生任何花费" in refusal and "不会被记成零" in refusal
    assert "正在执行的任务不会被中断" in refusal


def test_a_quiet_window_or_a_success_ends_the_run():
    breaker = CircuitBreaker()
    breaker.record_failure("x", now=0)
    breaker.record_failure("x", now=60)
    assert breaker.snapshot()["failures"] == 2
    # Weather that passed is not evidence about now.
    breaker.record_failure("x", now=60 + breaker.window + 1)
    assert breaker.snapshot()["failures"] == 1
    breaker.record_success(now=1000)
    assert breaker.state(1000) == "closed" and breaker.refusal(1000) is None
    assert breaker.snapshot()["failures"] == 0


def test_a_failed_probe_rearms_the_cooldown_instead_of_admitting_forever():
    """Half-open means "one attempt to see if it is back", not "the gate is off".

    Without the re-arm the breaker would sit half-open indefinitely after a long outage, admitting a
    paid attempt at every moment — exactly the load it exists to prevent.
    """
    breaker = CircuitBreaker()
    for index in range(BREAKER_FAILURE_THRESHOLD):
        breaker.record_failure("down", now=float(index))
    assert breaker.state(float(BREAKER_FAILURE_THRESHOLD)) == "open"
    later = BREAKER_FAILURE_THRESHOLD + breaker.cooldown
    assert breaker.state(later) == "half-open" and breaker.refusal(later) is None
    breaker.record_failure("still down", now=later)
    assert breaker.state(later) == "open", "a failed probe left the gate open"
    assert breaker.refusal(later) is not None
    assert breaker.state(later + breaker.cooldown - 1) == "open"
    assert breaker.state(later + breaker.cooldown) == "half-open"


def test_a_successful_probe_closes_it_and_says_who_did_it():
    breaker = CircuitBreaker()
    for index in range(BREAKER_FAILURE_THRESHOLD):
        breaker.record_failure("down", now=float(index))
    later = BREAKER_FAILURE_THRESHOLD + breaker.cooldown
    assert breaker.record_success(now=later + 1)["closed_by"] == "probe"
    assert breaker.state(later + 1) == "closed" and breaker.refusal(later + 1) is None


def test_a_stored_state_is_adopted_and_a_hand_edited_one_is_not_trusted():
    breaker = CircuitBreaker()
    for _ in range(BREAKER_FAILURE_THRESHOLD):
        breaker.record_failure("down")
    adopted = CircuitBreaker()
    adopted.load(breaker.snapshot())
    assert adopted.state() == "open" and adopted.refusal() is not None
    # A row edited by hand must not stop the service, and must not raise on the next request.
    junk = CircuitBreaker()
    junk.load({"failures": "很多", "opened_at": "昨天", "last_reason": {"a": 1}, "extra": "x"})
    assert junk.state() == "closed" and junk.refusal() is None
    assert junk.snapshot()["last_reason"] == str({"a": 1})
    assert "extra" not in junk.snapshot()


def test_the_breaker_survives_a_restart_so_restarting_is_not_a_way_around_it(tmp_path):
    """`start()` is what reads the stored state back, so this checks the boot path itself."""
    path = str(tmp_path / "restart.sqlite3")
    first = create_app(path, httpx.MockTransport(_refuse), quota=generous())
    with TestClient(first, headers=WRITE):
        assert open_breaker(first) == "open"

    second = create_app(path, httpx.MockTransport(_refuse), quota=generous())
    with TestClient(second, headers=WRITE) as client:
        assert breaker_state(client) == "open", "a restart reset the outage"
        refused = client.post("/api/agent/runs", json=GOAL)
        assert refused.status_code == 503, refused.text
        assert refused.headers["retry-after"]
        assert "没有产生任何花费" in refused.text
        assert client.get("/api/agent/runs").json() == [], "a refused submit still wrote a row"
    # Control: the state came from the database, not from an object both apps shared.
    fresh = create_app(str(tmp_path / "fresh.sqlite3"), httpx.MockTransport(_refuse), quota=generous())
    with TestClient(fresh, headers=WRITE) as client:
        assert breaker_state(client) == "closed"


def test_every_paid_entry_point_is_gated_not_only_the_first_one(tmp_path):
    """`resume` and a follow-up spend the same money, so they answer to the same refusal."""
    app = local_app(tmp_path, quota=generous(), name="gates")
    assert open_breaker(app) == "open"
    agent = app.state.agent
    calls = [lambda: agent.follow_up(FollowUpInput(parent_run="missing", goal="只保留有两篇的",
                                                   authorize_spend=True, consent_to_send=True),
                                     owner=LOCAL_OWNER),
             lambda: agent.retry(RetryInput(parent_run="missing", authorize_spend=True,
                                            consent_to_send=True), owner=LOCAL_OWNER),
             lambda: agent.resume("missing", owner=LOCAL_OWNER)]
    for call in calls:
        with pytest.raises(HTTPException) as caught:
            call()
        assert caught.value.status_code == 503, caught.value.detail


def test_only_the_destination_being_down_opens_it_for_everybody(tmp_path):
    """One account's wrong key is that account's problem.

    Counted the other way, a visitor who mistyped a key eight times would take the service away from
    everyone else — a denial of service with no cost to the attacker.
    """
    broken_key = Stub("denied")
    app = local_app(tmp_path, quota=generous(), model=broken_key, name="denied")
    with TestClient(app, headers=WRITE) as client:
        client.put("/api/agent/config", json=CONFIG)
        for _ in range(BREAKER_FAILURE_THRESHOLD * 2):
            assert client.post("/api/agent/config/test", json={}).status_code == 422
        assert breaker_state(client) == "closed"
        # ...and a run still starts, because nothing about the service is broken.
        assert client.post("/api/agent/runs", json=GOAL).status_code == 202
        release(app)

    outage = Stub("down")
    down = local_app(tmp_path, quota=generous(), model=outage, name="down")
    with TestClient(down, headers=WRITE) as client:
        client.put("/api/agent/config", json=CONFIG)
        for _ in range(BREAKER_FAILURE_THRESHOLD - 1):
            assert client.post("/api/agent/config/test", json={}).status_code == 422
        assert breaker_state(client) == "closed"
        assert client.post("/api/agent/config/test", json={}).status_code == 422
        assert breaker_state(client) == "open"
        refused = client.post("/api/agent/runs", json=GOAL)
        assert refused.status_code == 503 and "全站熔断已打开" in refused.text
        assert client.get("/api/agent/runs").json() == []
        # The connection test is the way out, so the breaker must not stand in its way.
        outage.outcome = "ok"
        assert client.post("/api/agent/config/test", json={}).status_code == 200
        assert breaker_state(client) == "closed"
        assert client.post("/api/agent/runs", json=GOAL).status_code == 202
        release(down)


def test_opening_the_breaker_does_not_interrupt_the_run_already_in_flight(tmp_path):
    """The breaker protects the process from *new* work; a call already sent may still be billed."""
    held = Stub("hold")
    app = local_app(tmp_path, quota=generous(), model=held, name="inflight")
    with TestClient(app, headers=WRITE) as client:
        client.put("/api/agent/config", json=CONFIG)
        rid = client.post("/api/agent/runs", json=GOAL).json()["id"]
        assert held.entered.wait(20)
        assert open_breaker(app) == "open"
        assert client.get(f"/api/agent/runs/{rid}").json()["status"] == "running"
        held.let_go.set()
        release(app)
        run = client.get(f"/api/agent/runs/{rid}").json()
        assert run["status"] == "failed", run
        assert run["model_calls"] == 1, "the turn was stopped rather than left to finish"
        assert "本轮到此结束" in run["error"]
        kinds = [event["kind"] for event in client.get(f"/api/agent/runs/{rid}/events").json()]
        assert "failed" in kinds and "cancelled" not in kinds


# -------------------------------------------------------------------------------- how it is read

def test_unset_limits_mean_ceilings_when_hosted_and_nothing_locally():
    assert quota_limits_from_env({}, hosted=True) == {
        "RE0_REQUESTS_PER_MINUTE": 120, "RE0_SITE_REQUESTS_PER_MINUTE": 600,
        "RE0_TASKS_PER_HOUR": 12}
    # A laptop with one reader at the keyboard: a number nobody chose must not fail a search.
    assert quota_limits_from_env({}, hosted=False) == {
        "RE0_REQUESTS_PER_MINUTE": 0, "RE0_SITE_REQUESTS_PER_MINUTE": 0, "RE0_TASKS_PER_HOUR": 0}
    # Explicit is honoured either way, which is how a local reader opts in.
    assert quota_limits_from_env({"RE0_TASKS_PER_HOUR": "2"}, hosted=False)["RE0_TASKS_PER_HOUR"] == 2


def test_a_negative_or_unparsable_limit_is_refused_rather_than_guessed():
    """Taken literally `-1` allows nothing; clamped, it allows everything. Neither is the intent."""
    with pytest.raises(ValueError, match="不能为负"):
        quota_limits_from_env({"RE0_TASKS_PER_HOUR": "-1"}, hosted=True)
    with pytest.raises(ValueError, match="必须是整数"):
        quota_limits_from_env({"RE0_REQUESTS_PER_MINUTE": "lots"}, hosted=True)
    assert quota_limits_from_env({"RE0_REQUESTS_PER_MINUTE": " 0 "},
                                 hosted=True)["RE0_REQUESTS_PER_MINUTE"] == 0


def test_doctor_prints_the_ceilings_and_a_bad_one_is_not_an_exit_zero(monkeypatch, capsys):
    """`re0 doctor` is what an operator runs, and `create_app` raises on the same number."""
    from re0 import cli

    for var in ("RE0_MODE", "RE0_SESSION_SECRET", "RE0_PUBLIC_ENTRY", "RE0_ALLOWED_ORIGINS", "RE0_STORAGE_MODE",
                "RE0_REQUESTS_PER_MINUTE", "RE0_SITE_REQUESTS_PER_MINUTE", "RE0_TASKS_PER_HOUR"):
        monkeypatch.delenv(var, raising=False)
    assert cli.main(["doctor"]) == 0
    ceiling = _ceiling_line(capsys)
    assert "每账户任务/小时 不限" in ceiling and "RE0_" not in ceiling
    assert "本地模式默认不限" in ceiling and "全站熔断" in ceiling

    monkeypatch.setenv("RE0_TASKS_PER_HOUR", "-3")
    assert cli.main(["doctor"]) == 2
    assert "配额配置无效" in capsys.readouterr().out

    monkeypatch.setenv("RE0_TASKS_PER_HOUR", "5")
    assert cli.main(["doctor"]) == 0
    ceiling = _ceiling_line(capsys)
    assert "每账户任务/小时 5" in ceiling and "已显式设置" in ceiling


def test_a_hosted_doctor_reports_the_defaults_it_will_enforce(monkeypatch, capsys):
    from re0 import cli

    for var in ("RE0_REQUESTS_PER_MINUTE", "RE0_SITE_REQUESTS_PER_MINUTE", "RE0_TASKS_PER_HOUR"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("RE0_MODE", "hosted")
    monkeypatch.setenv("RE0_SESSION_SECRET", "a-hosted-secret-that-is-long-enough-to-use")
    monkeypatch.setenv("RE0_PUBLIC_ENTRY", "https://re0.example.org")
    monkeypatch.setenv("RE0_ALLOWED_ORIGINS", "https://re0.example.org")
    monkeypatch.setenv("RE0_STORAGE_MODE", "persistent")
    assert cli.main(["doctor"]) == 0
    ceiling = _ceiling_line(capsys)
    assert "每账户请求/分钟 120" in ceiling and "每账户任务/小时 12" in ceiling
    assert "全站请求/分钟 600" in ceiling and "本地模式" not in ceiling


def _ceiling_line(capsys):
    out = capsys.readouterr().out
    return [line for line in out.splitlines() if "配额：" in line][0]


def test_every_ceiling_the_code_reads_is_documented_where_a_caller_looks():
    """A limit that exists in code and in no document is a limit nobody can configure.

    The person it just refused has nothing to read that explains it, and the operator tuning it has
    only the source. So the names `quota.py` reads, and the defaults it prints, are pinned to the two
    documents a caller actually opens.
    """
    import re
    from pathlib import Path
    import re0.quota as quota_module

    source = Path(quota_module.__file__).read_text(encoding="utf-8")
    names = set(re.findall(r"RE0_[A-Z_]+", source))
    assert names == {"RE0_REQUESTS_PER_MINUTE", "RE0_SITE_REQUESTS_PER_MINUTE", "RE0_TASKS_PER_HOUR"}
    root = Path(quota_module.__file__).parents[2]
    readme = (root / "README.md").read_text(encoding="utf-8")
    for document in ("README.md", "SECURITY.md"):
        text = (root / document).read_text(encoding="utf-8")
        for name in names:
            assert name in text, f"{name} is read by quota.py but missing from {document}"
    defaults = quota_limits_from_env({}, hosted=True)
    assert defaults == {"RE0_REQUESTS_PER_MINUTE": 120, "RE0_SITE_REQUESTS_PER_MINUTE": 600,
                        "RE0_TASKS_PER_HOUR": 12}
    for name, value in defaults.items():
        assert f"默认 **{value}**" in readme, f"{name}'s documented default is not {value}"


def test_a_runtime_built_without_explicit_ceilings_still_honours_the_deployment(tmp_path,
                                                                                monkeypatch):
    """Forgetting to pass the ceilings must not be a way to have none.

    `AgentRuntime` is constructed by `create_app` today, but a second construction site that omits the
    argument should inherit the declared environment rather than an unlimited default, which is the
    difference between one policy and two quietly disagreeing ones.
    """
    from re0.agent.runtime import AgentRuntime
    from re0.db import Database
    from re0.service import Store

    monkeypatch.setenv("RE0_TASKS_PER_HOUR", "3")
    runtime = AgentRuntime(Store(Database(str(tmp_path / "direct.sqlite3"))))
    assert runtime.quota.limits["RE0_TASKS_PER_HOUR"] == 3
    assert runtime.quota.mode == "enforced"
    # Local mode, and nothing else was set: the other two stay off, which is the documented default.
    assert runtime.quota.limits["RE0_REQUESTS_PER_MINUTE"] == 0


def test_what_a_caller_sees_is_their_own_usage_and_the_sites_state(tmp_path):
    """The ceilings are public; who has spent how much is not."""
    app = hosted_app(tmp_path, quota=generous(requests_per_minute=50), name="describe")
    a, b = browser(app, "henry"), browser(app, "iris")
    for _ in range(7):
        a.get("/api/papers")
    mine = a.get("/api/agent/config").json()["quota"]
    body = a.get("/api/agent/config").text
    theirs = b.get("/api/agent/config").json()["quota"]
    assert mine["requests_used"]["account"] >= 8
    assert mine["requests_used"]["site"] >= mine["requests_used"]["account"]
    assert mine["limits"]["RE0_TASKS_PER_HOUR"] == 500
    assert mine["breaker"]["threshold"] == BREAKER_FAILURE_THRESHOLD
    assert mine["breaker"]["state"] == "closed"
    assert "只有模型服务连续不可用会计入全站熔断" in mine["breaker"]["note"]
    assert theirs["requests_used"]["account"] < mine["requests_used"]["account"]
    with app.state.store.db.connect() as con:
        workspaces = [row[0] for row in con.execute("SELECT workspace FROM auth_accounts")]
    assert len(workspaces) == 2
    for workspace in workspaces:
        assert workspace not in body, "one account's usage is visible to another"
