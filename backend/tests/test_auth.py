"""Two identities, one process: what each may see, and what neither may see (#13).

These tests are the release gate the issue asks for, minus the second browser: two independent
sessions over one app, each with its own cookie jar. Everything here is offline — no model is called
unless a test says so, and no provider is contacted at all.
"""
import httpx
import pytest
from fastapi.testclient import TestClient
from datetime import timedelta

from re0.auth import (MAX_FAILED_ATTEMPTS, MAX_GUEST_SESSIONS, AccountStore, GuestCapacityError,
                      hash_password, new_secret, verify_password)
from re0.db import Database
from re0.deployment import DeploymentError, from_env, trusted_hosts_from_env
from re0.main import create_app
from re0.models import PaperInput
from re0.quota import destination_key

HOSTED = {"RE0_MODE": "hosted", "RE0_SESSION_SECRET": "a-hosted-secret-that-is-long-enough-to-use",
          "RE0_PUBLIC_ENTRY": "https://re0.example.org",
          "RE0_ALLOWED_ORIGINS": "https://re0.example.org",
          "RE0_STORAGE_MODE": "ephemeral-demo"}
WRITE = {"X-Re0-Client": "web", "Content-Type": "application/json"}
PASSWORD = "a-password-nobody-guesses"


def deployment(**extra):
    return from_env({**HOSTED, **extra}).require_startable()


def hosted(tmp_path, transport=None, **extra):
    """A hosted app and two sessions that do not share a cookie jar."""
    def refuse(request):
        raise AssertionError(f"Unexpected network request: {request.url}")
    app = create_app(str(tmp_path / "hosted.sqlite3"), transport or httpx.MockTransport(refuse),
                     deployment=deployment(**extra))
    return app


def session(app, username):
    """One logged-in browser: its own cookie jar, so its session is only its own.

    The base URL is https because a hosted session cookie is Secure, and an http client would
    silently drop it — which would look like a server that never sets one.
    """
    app.state.accounts.create_account(username, PASSWORD)
    client = TestClient(app, headers=WRITE, base_url="https://testserver")
    response = client.post("/api/auth/login", json={"username": username, "password": PASSWORD})
    assert response.status_code == 200, response.text
    return client


@pytest.fixture
def public_dns(monkeypatch):
    """Hosted mode resolves a model host before it dials; a test must not depend on this machine's resolver."""
    import socket

    def resolve(host, port, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))]

    monkeypatch.setattr(socket, "getaddrinfo", resolve)


def paper(client, title, **extra):
    response = client.post("/api/papers", json={"title": title, **extra})
    assert response.status_code == 201, response.text
    return response.json()


# ----------------------------------------------------------------------------------- deployment

def test_hosted_mode_refuses_to_start_and_names_everything_missing_at_once():
    """One start-up tells the operator the whole list, not one variable per restart."""
    with pytest.raises(DeploymentError) as caught:
        from_env({"RE0_MODE": "hosted"}).require_startable()
    message = str(caught.value)
    for required in ("RE0_SESSION_SECRET", "RE0_PUBLIC_ENTRY", "RE0_ALLOWED_ORIGINS", "RE0_STORAGE_MODE"):
        assert required in message, message
    assert "拒绝启动" in message


def test_a_documented_example_secret_is_not_a_secret():
    with pytest.raises(DeploymentError, match="示例值"):
        from_env({**HOSTED, "RE0_SESSION_SECRET": "changeme"}).require_startable()
    # Padding an example until it is long enough is what "太短" invites; it is still public knowledge.
    with pytest.raises(DeploymentError, match="示例值"):
        from_env({**HOSTED, "RE0_SESSION_SECRET": "changeme" * 8}).require_startable()
    with pytest.raises(DeploymentError, match="太短"):
        from_env({**HOSTED, "RE0_SESSION_SECRET": "short"}).require_startable()
    # A generated one is accepted, which is the only way this check is useful rather than ornamental.
    assert from_env({**HOSTED, "RE0_SESSION_SECRET": new_secret()}).require_startable().hosted


def test_hosted_mode_requires_an_explicit_storage_contract():
    with pytest.raises(DeploymentError, match="RE0_STORAGE_MODE"):
        from_env({key: value for key, value in HOSTED.items() if key != "RE0_STORAGE_MODE"}).require_startable()
    with pytest.raises(DeploymentError, match="ephemeral-demo.*persistent"):
        from_env({**HOSTED, "RE0_STORAGE_MODE": "guess"}).require_startable()
    assert from_env({**HOSTED, "RE0_STORAGE_MODE": "persistent"}).require_startable().storage_mode == "persistent"


def test_guest_access_is_opt_in_and_publicly_described():
    disabled = from_env(HOSTED).require_startable()
    enabled = from_env({**HOSTED, "RE0_GUEST_ACCESS": "true"}).require_startable()
    assert disabled.guest_access_enabled is False
    assert enabled.describe()["guest_access_enabled"] is True


def test_guest_sessions_are_distinct_bounded_revocable_and_not_admin_accounts(tmp_path):
    accounts = AccountStore(Database(str(tmp_path / "guests.sqlite3")))
    first, first_token = accounts.create_guest(max_active=1)
    assert first.kind == "guest" and first.authenticated
    assert accounts.resolve(first_token) == first
    assert accounts.owner_has_live_session(first.owner)
    assert accounts.count_accounts() == 0 and accounts.accounts() == []
    with pytest.raises(GuestCapacityError):
        accounts.create_guest(max_active=1)
    second, second_token = accounts.create_guest(ttl=timedelta(hours=2), max_active=2)
    assert first.owner != second.owner and first.user_id != second.user_id
    assert first_token != second_token
    assert accounts.revoke(first_token)
    assert accounts.resolve(first_token) is None
    assert not accounts.owner_has_live_session(first.owner)
    assert accounts.owner_has_live_session(second.owner)
    assert accounts.delete_guest_owner(first.owner)
    assert accounts.revoke(second_token) and accounts.delete_guest_owner(second.owner)
    assert accounts.create_guest(max_active=1)[0].kind == "guest"


def test_render_environment_supplies_only_its_declared_public_entry():
    deployment = from_env({
        "RE0_MODE": "hosted", "RE0_SESSION_SECRET": HOSTED["RE0_SESSION_SECRET"],
        "RE0_STORAGE_MODE": "ephemeral-demo", "RENDER": "true",
        "RENDER_EXTERNAL_URL": "https://re0-render.onrender.com",
        "RENDER_EXTERNAL_HOSTNAME": "re0-render.onrender.com"}).require_startable()
    assert deployment.public_entry == "https://re0-render.onrender.com"
    assert deployment.allowed_origins == ("https://re0-render.onrender.com",)
    assert deployment.storage_mode == "ephemeral-demo"


def test_explicit_render_entry_and_origin_override_platform_defaults():
    configured = from_env({
        "RE0_MODE": "hosted", "RE0_SESSION_SECRET": HOSTED["RE0_SESSION_SECRET"],
        "RE0_STORAGE_MODE": "persistent", "RENDER": "true",
        "RENDER_EXTERNAL_URL": "https://auto.onrender.com",
        "RENDER_EXTERNAL_HOSTNAME": "auto.onrender.com",
        "RE0_PUBLIC_ENTRY": "https://custom.example.org",
        "RE0_ALLOWED_ORIGINS": "https://custom.example.org,https://admin.example.org",
    }).require_startable()
    assert configured.public_entry == "https://custom.example.org"
    assert configured.allowed_origins == ("https://custom.example.org", "https://admin.example.org")
    assert configured.storage_mode == "persistent"


def test_render_platform_entry_is_not_guessed_outside_render_or_from_a_bad_url():
    base = {"RE0_MODE": "hosted", "RE0_SESSION_SECRET": HOSTED["RE0_SESSION_SECRET"],
            "RE0_STORAGE_MODE": "ephemeral-demo", "RENDER_EXTERNAL_URL": "https://auto.onrender.com"}
    with pytest.raises(DeploymentError, match="RE0_PUBLIC_ENTRY"):
        from_env(base).require_startable()
    with pytest.raises(DeploymentError, match="RE0_PUBLIC_ENTRY"):
        from_env({**base, "RENDER": "true", "RENDER_EXTERNAL_URL": "http://auto.onrender.com"}).require_startable()


def test_render_host_allowlist_uses_only_platform_values_and_honors_override():
    render = {"RENDER": "true", "RENDER_EXTERNAL_HOSTNAME": "re0-render.onrender.com"}
    assert trusted_hosts_from_env(render) == (
        "localhost", "127.0.0.1", "[::1]", "testserver", "re0-render.onrender.com")
    assert trusted_hosts_from_env({"RENDER": "true", "RENDER_EXTERNAL_HOSTNAME": "re0-render.onrender.com",
                                   "RE0_ALLOWED_HOSTS": "custom.example.org"}) == ("custom.example.org",)
    assert "re0-render.onrender.com" not in trusted_hosts_from_env(
        {"RENDER_EXTERNAL_HOSTNAME": "re0-render.onrender.com"})


def test_render_host_is_accepted_but_an_unlisted_host_is_refused(tmp_path, monkeypatch):
    monkeypatch.setenv("RENDER", "true")
    monkeypatch.setenv("RENDER_EXTERNAL_HOSTNAME", "re0-render.onrender.com")
    monkeypatch.delenv("RE0_ALLOWED_HOSTS", raising=False)
    client = TestClient(hosted(tmp_path), base_url="https://re0-render.onrender.com")
    health = client.get("/api/health")
    assert health.status_code == 200, health.text
    assert health.json()["storage_mode"] == "ephemeral-demo"
    refused = client.get("/api/health", headers={"host": "attacker.invalid"})
    assert refused.status_code == 400


def test_an_http_entry_or_an_insecure_cookie_override_is_refused():
    with pytest.raises(DeploymentError, match="https"):
        from_env({**HOSTED, "RE0_PUBLIC_ENTRY": "http://re0.example.org",
                  "RE0_ALLOWED_ORIGINS": "http://re0.example.org"}).require_startable()
    with pytest.raises(DeploymentError, match="Secure"):
        from_env({**HOSTED, "RE0_ALLOW_INSECURE_COOKIES": "1"}).require_startable()


def test_a_loopback_origin_is_refused_in_hosted_mode():
    """Every visitor has their own localhost, so allowing one allows any of them."""
    with pytest.raises(DeploymentError, match="本机来源"):
        from_env({**HOSTED,
                  "RE0_ALLOWED_ORIGINS": "https://re0.example.org,https://localhost:3000"}
                 ).require_startable()


def test_an_entry_that_is_not_among_the_allowed_origins_is_a_configuration_error():
    """The public door refusing itself is a typo, and it is reported as one rather than served."""
    with pytest.raises(DeploymentError, match="不在 RE0_ALLOWED_ORIGINS"):
        from_env({**HOSTED, "RE0_ALLOWED_ORIGINS": "https://other.example.org"}).require_startable()


def test_local_mode_needs_no_configuration_and_does_not_require_a_session(tmp_path):
    with pytest.raises(DeploymentError):
        from_env({"RE0_MODE": "hosted-please"}).require_startable()
    app = create_app(str(tmp_path / "local.sqlite3"), httpx.MockTransport(
        lambda request: (_ for _ in ()).throw(Assertion("no network"))))
    client = TestClient(app, headers=WRITE)
    assert client.get("/api/health").json()["mode"] == "local"
    assert client.get("/api/health").json()["auth_required"] is False
    # No login, and the data belongs to the one owner a local database has.
    assert paper(client, "本地的一篇")["title"] == "本地的一篇"
    assert client.post("/api/auth/login",
                       json={"username": "alice", "password": PASSWORD}).status_code == 409


# ------------------------------------------------------------------------------------ credentials

def test_passwords_are_salted_hashed_and_verified_in_constant_time_terms(tmp_path):
    store = AccountStore(Database(str(tmp_path / "auth.sqlite3")))
    account = store.create_account("alice", PASSWORD)
    stored = [row for row in store.accounts() if row["user_id"] == account["user_id"]][0]
    assert "password" not in stored and "hash" not in str(stored)
    assert store.verify("alice", PASSWORD) == account["user_id"]
    assert store.verify("alice", "wrong") is None
    assert store.verify("nobody", PASSWORD) is None
    # A hash is not a password, and two hashes of one password are not the same string.
    assert hash_password(PASSWORD) != hash_password(PASSWORD)
    assert verify_password(PASSWORD, hash_password(PASSWORD))
    assert not verify_password(PASSWORD, "pbkdf2_sha256$1$00$00")


def test_the_local_owner_name_cannot_be_taken_by_an_account(tmp_path):
    """`local` owns every row a single-user database already holds."""
    store = AccountStore(Database(str(tmp_path / "auth.sqlite3")))
    for name in ("local", "localhost", "local-owner"):
        with pytest.raises(Exception, match="保留名"):
            store.create_account(name, PASSWORD)


def test_repeated_failures_lock_the_account_and_a_lock_is_not_a_wrong_password(tmp_path):
    store = AccountStore(Database(str(tmp_path / "auth.sqlite3")))
    store.create_account("alice", PASSWORD)
    for _ in range(MAX_FAILED_ATTEMPTS):
        assert store.verify("alice", "wrong") is None
    # Locked: even the right password is refused until the lock expires.
    assert store.verify("alice", PASSWORD) is None
    assert [row for row in store.accounts() if row["username"] == "alice"][0]["locked_until"]


def test_a_revoked_or_expired_session_stops_working_immediately(tmp_path):
    from datetime import timedelta

    store = AccountStore(Database(str(tmp_path / "auth.sqlite3")))
    account = store.create_account("alice", PASSWORD)
    token, _ = store.issue(store.verify("alice", PASSWORD))
    assert store.resolve(token).owner == account["workspace"]
    assert store.owner_has_live_session(account["workspace"])
    assert store.revoke(token)
    assert store.resolve(token) is None
    assert not store.owner_has_live_session(account["workspace"])
    short, _ = store.issue(account["user_id"], ttl=timedelta(seconds=-1))
    assert store.resolve(short) is None
    # Disabling an account ends the sessions it already had, rather than waiting for them to expire.
    live, _ = store.issue(account["user_id"])
    store.set_disabled(account["user_id"], True)
    assert store.resolve(live) is None
    assert not store.owner_has_live_session(account["workspace"])


def test_model_list_probe_uses_hosted_destination_policy(tmp_path, monkeypatch):
    import socket

    monkeypatch.setattr(socket, "getaddrinfo",
                        lambda host, port, **kw: [(socket.AF_INET, socket.SOCK_STREAM, 6, "",
                                                   ("198.18.1.7", 443))])
    requests = []

    def network(request):
        requests.append(request)
        return httpx.Response(200, json={"data": [{"id": "must-not-be-reached"}]})

    app = hosted(tmp_path, httpx.MockTransport(network))
    alice = session(app, "alice")
    for base_url in ("http://127.0.0.1:11434/v1", "https://api.openai.com/v1"):
        response = alice.post("/api/agent/models", json={
            "base_url": base_url, "api_key": "sentinel-model-list-key", "trust_endpoint": True})
        assert response.status_code == 422, response.text
    assert requests == [], "the hosted probe must refuse before opening any connection"


def test_model_list_429_cooldown_is_scoped_to_owner_and_honours_retry_after(tmp_path, public_dns):
    calls = []

    def network(request):
        calls.append(request.headers.get("authorization", ""))
        if request.headers.get("authorization") == "Bearer sk-alice-only":
            return httpx.Response(429, headers={"Retry-After": "11"}, json={"error": "rate limited"})
        return httpx.Response(200, json={"data": [{"id": "bob-model"}]})

    app = hosted(tmp_path, httpx.MockTransport(network))
    alice, bob = session(app, "alice"), session(app, "bob")
    body = {"base_url": "https://api.openai.com/v1", "trust_endpoint": True}
    refused = alice.post("/api/agent/models", json={**body, "api_key": "sk-alice-only"})
    assert refused.status_code == 429 and refused.headers["retry-after"] == "11"
    assert "sk-alice-only" not in refused.text
    succeeded = bob.post("/api/agent/models", json={**body, "api_key": "sk-bob-only"})
    assert succeeded.status_code == 200 and succeeded.json()["models"] == ["bob-model"]
    repeated = alice.post("/api/agent/models", json={**body, "api_key": "sk-alice-only"})
    assert repeated.status_code == 429
    assert len(calls) == 2, "Alice's cooldown must refuse before another provider request"
    assert app.state.agent.quota.destination_breakers.state(
        destination_key(body["base_url"])) == "closed"
    alice_cooldowns = alice.get("/api/agent/config").json()["quota"]["probes"]["cooldowns"]
    assert alice_cooldowns and alice_cooldowns[0]["retry_after"] > 0
    assert bob.get("/api/agent/config").json()["quota"]["probes"]["cooldowns"] == []


@pytest.mark.parametrize("path", ["models", "config", "test", "run"])
def test_all_hosted_model_paths_reject_private_or_loopback_before_network(tmp_path, monkeypatch, path):
    import time
    from re0.agent.model import ModelError

    requests = []

    def network(request):
        requests.append(request)
        raise AssertionError("refused model destination must not reach the transport")

    def private_resolution(hostname):
        raise ModelError("fixture refused private DNS resolution")

    monkeypatch.setattr("re0.agent.model._require_public_resolution", private_resolution)
    app = hosted(tmp_path, httpx.MockTransport(network))
    alice = session(app, "alice")
    if path == "models":
        response = alice.post("/api/agent/models", json={
            "base_url": "http://127.0.0.1:11434/v1", "api_key": "sentinel-key",
            "trust_endpoint": True})
        assert response.status_code == 422, response.text
    elif path == "config":
        response = alice.put("/api/agent/config", json={
            "base_url": "http://127.0.0.1:11434/v1", "model": "fixture-model",
            "api_key": "sentinel-key", "trust_endpoint": True})
        assert response.status_code == 422, response.text
    else:
        # Accept during configuration, then model a DNS answer changing before the actual call.
        monkeypatch.setattr("re0.agent.model._require_public_resolution", lambda hostname: None)
        response = alice.put("/api/agent/config", json={
            "base_url": "https://api.openai.com/v1", "model": "fixture-model",
            "api_key": "sentinel-key", "trust_endpoint": True})
        assert response.status_code == 200, response.text
        monkeypatch.setattr("re0.agent.model._require_public_resolution", private_resolution)
        if path == "test":
            response = alice.post("/api/agent/config/test", json={})
            assert response.status_code == 422, response.text
        else:
            response = alice.post("/api/agent/runs", json={
                "goal": "Check one paper resource", "consent_to_send": True,
                "max_model_calls": 2, "max_tool_calls": 2, "attempt_seconds": 30,
                "use_library": False})
            assert response.status_code == 202, response.text
            rid = response.json()["id"]
            deadline = time.monotonic() + 5
            run = app.state.agent.tasks.get(rid, owner=alice.get("/api/auth/session").json()
                                            ["identity"]["workspace"], internal=True)
            while run["status"] in {"queued", "running"} and time.monotonic() < deadline:
                time.sleep(0.01)
                run = app.state.agent.tasks.get(rid, owner=run["owner"], internal=True)
            assert run["status"] == "failed", run["status"]
    assert requests == [], "all four model paths must refuse before opening a connection"


# ---------------------------------------------------------------------------------- the HTTP gate

def test_every_api_route_needs_a_session_in_hosted_mode(tmp_path):
    app = hosted(tmp_path)
    client = TestClient(app, headers=WRITE)
    for path in ("/api/papers", "/api/topics", "/api/knowledge/templates", "/api/export", "/api/agent/runs",
                 "/api/agent/conversations", "/api/zotero/status?library_id=12345",
                 "/api/agent/config"):
        assert client.get(path).status_code == 401, path
    for path, body in (("/api/papers", {"title": "x"}), ("/api/demo", {})):
        assert client.post(path, json=body).status_code == 401, path
    # The shell still loads, or nobody could reach the login form; health answers for orchestrators.
    assert client.get("/api/health").status_code == 200
    assert client.get("/library").status_code == 200


def test_login_logout_and_who_am_i(tmp_path):
    app = hosted(tmp_path)
    alice = session(app, "alice")
    me = alice.get("/api/auth/session").json()
    assert me["identity"]["authenticated"] is True and me["identity"]["user_id"]
    assert me["deployment"]["mode"] == "hosted"
    assert me["deployment"]["storage_mode"] == "ephemeral-demo"
    # The cookie is HttpOnly and SameSite=Strict, and Secure because the entry is https.
    cookie = [item for item in alice.cookies.jar][0]
    assert cookie.name == "re0_session"
    headers = alice.post("/api/auth/logout").json()
    assert headers["revoked"] is True
    assert alice.get("/api/papers").status_code == 401
    assert alice.post("/api/auth/logout").status_code == 401


@pytest.mark.parametrize("revoke_via", ["logout", "operator", "disable", "expiry"])
def test_session_revoke_or_key_expiry_stops_the_next_model_call(tmp_path, public_dns, revoke_via):
    import json
    import threading
    import time

    entered, release = threading.Event(), threading.Event()
    model_requests = []
    sentinel = "sentinel-key-cleared-on-logout"

    def network(request):
        if request.url.path.endswith("/chat/completions"):
            model_requests.append(request)
            entered.set()
            assert release.wait(5), "test did not release the in-flight model call"
            return httpx.Response(200, json={"choices": [{"finish_reason": "tool_calls", "message": {
                "role": "assistant", "content": "", "tool_calls": [{"id": "call_plan", "type": "function",
                    "function": {"name": "update_plan", "arguments": json.dumps({"steps": ["search"]})}}]}}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}})
        raise AssertionError(f"unexpected outbound request: {request.url}")

    app = create_app(str(tmp_path / "logout-key.sqlite3"), httpx.MockTransport(network),
                     deployment=deployment())
    app.state.accounts.create_account("alice", PASSWORD)
    with TestClient(app, headers=WRITE, base_url="https://testserver") as alice:
        login = alice.post("/api/auth/login", json={"username": "alice", "password": PASSWORD})
        assert login.status_code == 200
        owner = login.json()["identity"]["workspace"]
        user_id = login.json()["identity"]["user_id"]
        configured = alice.put("/api/agent/config", json={
            "base_url": "https://api.openai.com/v1", "model": "fixture-model",
            "api_key": sentinel, "trust_endpoint": True})
        assert configured.status_code == 200, configured.text
        created = alice.post("/api/agent/runs", json={
            "goal": "Check one paper resource", "consent_to_send": True,
            "max_model_calls": 3, "max_tool_calls": 3, "attempt_seconds": 60,
            "use_library": False})
        assert created.status_code == 202, created.text
        rid = created.json()["id"]
        assert entered.wait(5), "the fixture model call did not start"

        if revoke_via == "logout":
            logged_out = alice.post("/api/auth/logout")
            assert logged_out.status_code == 200 and logged_out.json()["revoked"] is True
            assert logged_out.json()["credentials_cleared"] is True
            assert app.state.agent.vault.public(owner=owner)["configured"] is False
        else:
            if revoke_via == "operator":
                # The operator CLI revokes rows from a separate process; the worker checks the DB
                # before the next call and lazily discards the now-unusable memory copy.
                assert app.state.accounts.revoke_user(user_id) == 1
            elif revoke_via == "disable":
                app.state.accounts.set_disabled(user_id, True)
                assert not app.state.accounts.owner_has_live_session(owner)
            else:
                # Expire the vault's monotonic lease while the first request is already in flight.
                app.state.agent.vault._deadlines[owner] = time.monotonic() - 1
        release.set()

        deadline = time.monotonic() + 5
        run = app.state.agent.tasks.get(rid, owner=owner, internal=True)
        while run["status"] in {"queued", "running"} and time.monotonic() < deadline:
            time.sleep(0.01)
            run = app.state.agent.tasks.get(rid, owner=owner, internal=True)
        assert run["status"] == "cancelled", run["status"]
        assert app.state.agent.vault.public(owner=owner)["configured"] is False
        assert run["state"]["model_calls"] == 1
        assert len(model_requests) == 1, "logout may let the in-flight call finish, never start another"
        assert sentinel not in json.dumps(run, ensure_ascii=False)


def test_logout_clears_only_the_authenticated_owners_model_key(tmp_path, public_dns):
    app = hosted(tmp_path)
    alice, bob = session(app, "alice"), session(app, "bob")
    config = {"base_url": "https://api.openai.com/v1", "model": "fixture-model",
              "api_key": "owner-scoped-sentinel", "trust_endpoint": True}
    assert alice.put("/api/agent/config", json=config).status_code == 200
    assert bob.put("/api/agent/config", json=config).status_code == 200
    alice_owner = alice.get("/api/auth/session").json()["identity"]["workspace"]
    bob_owner = bob.get("/api/auth/session").json()["identity"]["workspace"]

    assert alice.post("/api/auth/logout").json()["credentials_cleared"] is True
    assert app.state.agent.vault.public(owner=alice_owner)["configured"] is False
    assert app.state.agent.vault.public(owner=bob_owner)["configured"] is True


def test_one_authenticated_tab_logout_clears_key_for_the_owners_other_tab(tmp_path, public_dns):
    def unexpected(request):
        raise AssertionError(f"no model request expected: {request.url}")

    app = hosted(tmp_path, httpx.MockTransport(unexpected))
    app.state.accounts.create_account("alice", PASSWORD)
    first = TestClient(app, headers=WRITE, base_url="https://testserver")
    second = TestClient(app, headers=WRITE, base_url="https://testserver")
    for client in (first, second):
        response = client.post("/api/auth/login", json={"username": "alice", "password": PASSWORD})
        assert response.status_code == 200, response.text
    owner = first.get("/api/auth/session").json()["identity"]["workspace"]
    assert owner == second.get("/api/auth/session").json()["identity"]["workspace"]
    assert first.put("/api/agent/config", json={
        "base_url": "https://api.openai.com/v1", "model": "fixture-model",
        "api_key": "same-owner-tab-key", "trust_endpoint": True}).status_code == 200

    logged_out = first.post("/api/auth/logout")
    assert logged_out.status_code == 200 and logged_out.json()["credentials_cleared"] is True
    assert second.get("/api/auth/session").json()["identity"]["authenticated"] is True
    assert second.get("/api/agent/config").json()["configured"] is False
    assert app.state.agent.vault.public(owner=owner)["configured"] is False


def test_login_after_operator_revocation_does_not_reuse_the_previous_memory_key(tmp_path, public_dns):
    app = hosted(tmp_path)
    alice = session(app, "alice")
    identity = alice.get("/api/auth/session").json()["identity"]
    configured = alice.put("/api/agent/config", json={
        "base_url": "https://api.openai.com/v1", "model": "fixture-model",
        "api_key": "revoked-account-key", "trust_endpoint": True})
    assert configured.status_code == 200
    assert app.state.accounts.revoke_user(identity["user_id"]) == 1

    with TestClient(app, headers=WRITE, base_url="https://testserver") as next_session:
        login = next_session.post("/api/auth/login", json={"username": "alice", "password": PASSWORD})
        assert login.status_code == 200, login.text
        assert next_session.get("/api/agent/config").json()["configured"] is False
    assert app.state.agent.vault.public(owner=identity["workspace"])["configured"] is False


def test_two_accounts_do_not_see_each_others_library_tasks_or_mappings(tmp_path):
    app = hosted(tmp_path)
    alice, bob = session(app, "alice"), session(app, "bob")
    mine = paper(alice, "Alice 的一篇", doi="10.1000/shared")
    alice.post(f"/api/papers/{mine['id']}/resources",
               json={"kind": "code", "label": "repo", "url": "https://github.com/a/b"})
    alice_knowledge = alice.get(f"/api/papers/{mine['id']}/knowledge")
    assert alice_knowledge.status_code == 200 and alice_knowledge.json()["versions"]
    assert bob.get(f"/api/papers/{mine['id']}/knowledge").status_code == 404
    template = next(row for row in alice.get("/api/knowledge/templates").json()
                    if row["template_key"] == "layout-layer")
    definition = dict(template["definition"])
    definition["concepts"] = [*definition["concepts"], {
        "key": "alice_private_term", "kind": "method", "label": "Alice private term",
        "aliases": [], "parent": "layout_and_layers"}]
    custom_template = alice.post("/api/knowledge/templates/layout-layer", json=definition)
    assert custom_template.status_code == 201, custom_template.text
    assert custom_template.json()["id"] not in {
        row["id"] for row in bob.get("/api/knowledge/templates").json()}
    assert len(alice.get("/api/papers").json()) == 1

    assert bob.get("/api/papers").json() == []
    assert bob.get("/api/papers").status_code == 200
    # Enumeration is answered with the same 404 a missing id gets: a 403 would confirm it exists.
    assert bob.get(f"/api/papers/{mine['id']}").status_code == 404
    resource_id = alice.get("/api/papers").json()[0]["resources"][0]["id"]
    assert bob.get(f"/api/resources/{resource_id}/observations").status_code == 404
    assert bob.delete(f"/api/resources/{resource_id}").status_code == 404
    assert bob.delete(f"/api/papers/{mine['id']}").status_code == 404
    # Nothing was touched by the attempts.
    assert len(alice.get(f"/api/papers/{mine['id']}").json()["resources"]) == 1
    # The same DOI is Bob's to add: a shared identifier is not a shared record, and the refusal that
    # used to say "already in the library" told Bob that Alice had it.
    assert paper(bob, "Bob 的一篇", doi="10.1000/shared")["doi"] == "10.1000/shared"
    assert len(bob.get("/api/papers").json()) == 1
    # A direction is per-account too: a name one of them adds is not the other's to see.
    alice.post("/api/topics", json={"name": "只有 Alice 的方向"})
    assert "只有 Alice 的方向" in alice.get("/api/topics").json()
    assert "只有 Alice 的方向" not in bob.get("/api/topics").json()
    assert "Alice" not in bob.get("/api/export").text
    assert bob.get("/api/agent/runs").json() == []


def test_imported_workspaces_are_scoped_by_the_authenticated_owner(tmp_path):
    from re0.workspace import Workspace, managed_workspace_path

    app = hosted(tmp_path)
    alice, bob = session(app, "alice"), session(app, "bob")
    source = Workspace(tmp_path / "external-workspace").open()
    source.record({"source_url": "https://export.arxiv.org/abs/2501.12345",
                   "locator": "metadata", "kind": "paper", "content": "fixture"},
                  tool="search_papers")
    bundle = source.bundle()
    assert alice.post("/api/workspaces/import", json={"bundle": bundle}).status_code == 201
    assert len(alice.get("/api/workspaces").json()["workspaces"]) == 1
    assert bob.get("/api/workspaces").json()["workspaces"] == []
    alice_owner = alice.get("/api/auth/session").json()["identity"]["workspace"]
    bob_owner = bob.get("/api/auth/session").json()["identity"]["workspace"]
    alice_path = managed_workspace_path(app.state.store.db.path, alice_owner, source.workspace_id)
    bob_path = managed_workspace_path(app.state.store.db.path, bob_owner, source.workspace_id)
    assert alice_path.is_dir() and not bob_path.exists()
    assert bob.get(f"/api/workspaces/{source.workspace_id}/export").status_code == 404


def test_fulltext_workspace_import_cannot_cross_account_or_paper_scope(tmp_path):
    from re0.workspace import Workspace

    app = hosted(tmp_path)
    alice, bob = session(app, "alice"), session(app, "bob")
    paper_row = paper(alice, "Alice full-text paper", arxiv_id="2501.12345v1", version_label="v1")
    version = alice.get(f"/api/papers/{paper_row['id']}/knowledge").json()["versions"][0]
    source = Workspace(tmp_path / "external-fulltext").open()
    url = "https://arxiv.org/html/2501.12345v1"
    fulltext = {"source": "arxiv", "identifier": "2501.12345v1", "version": "v1",
                "state": "ok", "content_type": "text/html", "parser": "html.parser",
                "parser_version": "1", "source_url": url, "final_url": url,
                "fetched_at": "2026-09-23T00:00:00+00:00", "parse_quality": "ok"}
    source_id = source.record({"source_url": url, "locator": "§1¶1", "kind": "fulltext_chunk",
                               "content": "[§1¶1] This is a full-text fixture paragraph.",
                               "paper": {"identifier": "2501.12345v1", "arxiv_id": "2501.12345v1",
                                         "version": "v1"},
                               "fulltext": fulltext}, tool="fetch_paper_text")
    bundle = source.bundle()
    assert alice.post("/api/workspaces/import", json={"bundle": bundle}).status_code == 201
    request = {"workspace_id": source.workspace_id, "source_ids": [source_id],
               "paper_version_id": version["id"]}
    preview_path = f"/api/papers/{paper_row['id']}/knowledge/fulltext/preview"
    assert alice.post(preview_path, json=request).status_code == 200
    assert bob.get("/api/workspaces").json()["workspaces"] == []
    assert bob.post(preview_path, json=request).status_code == 404
    assert bob.post(f"/api/papers/{paper_row['id']}/knowledge/fulltext/import",
                    json=request).status_code == 404
    assert not any(item["snapshot_kind"] == "fulltext_chunk"
                   for item in alice.get(f"/api/papers/{paper_row['id']}/knowledge").json()["source_snapshots"])


def test_task_defaults_and_session_caps_are_per_account(tmp_path):
    app = hosted(tmp_path)
    alice, bob = session(app, "alice"), session(app, "bob")
    response = alice.put("/api/agent/defaults", json={"max_model_calls": 18, "max_tool_calls": 33})
    assert response.status_code == 200, response.text
    assert alice.get("/api/agent/config").json()["task_defaults"]["max_model_calls"] == 18
    # Bob's budget is his own: Alice raising hers must not widen what his tasks may spend.
    assert bob.get("/api/agent/config").json()["task_defaults"]["max_model_calls"] != 18


def test_a_model_key_belongs_to_the_account_that_entered_it(tmp_path, public_dns):
    app = hosted(tmp_path)
    alice, bob = session(app, "alice"), session(app, "bob")
    assert alice.get("/api/agent/config").json()["configured"] is False
    response = alice.put("/api/agent/config", json={"base_url": "https://api.openai.com/v1",
                                                    "model": "gpt-4o-mini", "api_key": "sk-alice-only",
                                                    "trust_endpoint": True})
    assert response.status_code == 200, response.text
    assert alice.get("/api/agent/config").json()["configured"] is True
    # Bob sees his own empty configuration, not Alice's destination — and never her key.
    assert bob.get("/api/agent/config").json()["configured"] is False
    assert "sk-alice-only" not in bob.get("/api/agent/config").text
    assert "sk-alice-only" not in alice.get("/api/agent/config").text
    assert "api.openai.com" not in bob.get("/api/agent/config").json().get("base_url", "")
    # Clearing Bob's configuration cannot clear Alice's.
    bob.delete("/api/agent/config")
    assert alice.get("/api/agent/config").json()["configured"] is True


def test_hosted_mode_refuses_a_request_claiming_to_be_the_console(tmp_path):
    app = hosted(tmp_path)
    alice = session(app, "alice")
    response = alice.post("/api/papers", json={"title": "x"},
                          headers={"X-Re0-Client": "cli", "Content-Type": "application/json"})
    assert response.status_code == 403
    assert "CLI" in response.json()["detail"]


def test_a_cross_origin_write_is_refused_and_an_allowed_origin_is_not(tmp_path):
    app = hosted(tmp_path)
    alice = session(app, "alice")
    evil = dict(WRITE, Origin="https://evil.example.org")
    assert alice.post("/api/papers", json={"title": "x"}, headers=evil).status_code == 403
    good = dict(WRITE, Origin="https://re0.example.org")
    assert alice.post("/api/papers", json={"title": "y"}, headers=good).status_code == 201


def test_a_body_cannot_declare_an_owner(tmp_path):
    """The owner comes from the session, so a payload that names one is describing nothing real."""
    app = hosted(tmp_path)
    alice, bob = session(app, "alice"), session(app, "bob")
    mine = paper(bob, "Bob 的一篇")
    # An extra field is refused by the model, and even a route that accepted one would read the
    # identity, not the body.
    assert alice.put(f"/api/papers/{mine['id']}",
                     json={"title": "改写", "owner": bob.get("/api/auth/session").json()
                           ["identity"]["workspace"]}).status_code in (404, 422)
    assert bob.get(f"/api/papers/{mine['id']}").json()["title"] == "Bob 的一篇"


def test_hosted_mode_refuses_a_loopback_model_destination(tmp_path):
    """A local Ollama is a single-user convenience, not something a public service dials for you."""
    app = hosted(tmp_path)
    alice = session(app, "alice")
    response = alice.put("/api/agent/config", json={"base_url": "http://127.0.0.1:11434/v1",
                                                    "model": "qwen2.5", "api_key": "",
                                                    "trust_endpoint": True})
    assert response.status_code in (400, 422), response.text
    assert alice.get("/api/agent/config").json()["configured"] is False
    # The same destination is fine locally, which is the difference between the two modes.
    local = TestClient(create_app(str(tmp_path / "local2.sqlite3"),
                                  httpx.MockTransport(lambda request: None)), headers=WRITE)
    assert local.put("/api/agent/config", json={"base_url": "http://127.0.0.1:11434/v1",
                                                "model": "qwen2.5", "api_key": "",
                                                "trust_endpoint": True}).status_code == 200


def test_a_hosted_refusal_does_not_advertise_an_opt_out_that_does_not_apply(tmp_path, monkeypatch):
    """A private resolution is refused in hosted mode even with the local-proxy variable set.

    The message matters as much as the refusal: the full-text path tells a single user how to opt in,
    and repeating that advice here would send an operator chasing a setting the mode ignores.
    """
    import socket

    monkeypatch.setenv("RE0_ALLOW_LOCAL_RESOLVER", "1")
    monkeypatch.setattr(socket, "getaddrinfo",
                        lambda host, port, **kw: [(socket.AF_INET, socket.SOCK_STREAM, 6, "",
                                                   ("198.18.1.7", 443))])
    alice = session(hosted(tmp_path), "alice")
    response = alice.put("/api/agent/config", json={"base_url": "https://api.openai.com/v1",
                                                    "model": "gpt-4o-mini", "api_key": "sk-x",
                                                    "trust_endpoint": True})
    assert response.status_code == 422, response.text
    detail = response.json()["detail"]
    assert "198.18.1.7" in detail and "不适用" in detail, detail
    assert "可以设" not in detail, detail
    assert alice.get("/api/agent/config").json()["configured"] is False


def test_the_launcher_checks_the_deployment_before_anything_listens(monkeypatch):
    """`RE0_HOST` is set where the service is started, so that is where it has to be refused.

    A traceback from inside uvicorn's import would still stop the service, but it arrives after the
    operator has read a stack frame rather than a reason, and the reason is the part they need.
    """
    import importlib.util
    from pathlib import Path

    spec = importlib.util.spec_from_file_location("re0_run", Path(__file__).resolve().parents[2] / "run.py")
    launcher = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(launcher)

    for var in ("RE0_MODE", "RE0_SESSION_SECRET", "RE0_PUBLIC_ENTRY", "RE0_ALLOWED_ORIGINS", "RE0_STORAGE_MODE"):
        monkeypatch.delenv(var, raising=False)
    launcher.check_deployment("127.0.0.1")
    with pytest.raises(SystemExit) as bound:
        launcher.check_deployment("0.0.0.0")
    assert "RE0_HOST" in str(bound.value) and "RE0_MODE=hosted" in str(bound.value)
    # A hosted start with nothing configured is refused too, and the public bind is then fine.
    monkeypatch.setenv("RE0_MODE", "hosted")
    with pytest.raises(SystemExit) as missing:
        launcher.check_deployment("0.0.0.0")
    for required in ("RE0_SESSION_SECRET", "RE0_PUBLIC_ENTRY", "RE0_ALLOWED_ORIGINS", "RE0_STORAGE_MODE"):
        assert required in str(missing.value), missing.value
    monkeypatch.setenv("RE0_SESSION_SECRET", new_secret())
    monkeypatch.setenv("RE0_PUBLIC_ENTRY", "https://re0.example.org")
    monkeypatch.setenv("RE0_ALLOWED_ORIGINS", "https://re0.example.org")
    monkeypatch.setenv("RE0_STORAGE_MODE", "persistent")
    launcher.check_deployment("0.0.0.0")


def test_doctor_says_which_mode_would_run_and_exits_non_zero_when_it_could_not(monkeypatch, capsys):
    """`doctor` is what an operator checks first, so "could not start" cannot be an exit-0 line."""
    from re0 import cli

    for var in ("RE0_MODE", "RE0_SESSION_SECRET", "RE0_PUBLIC_ENTRY", "RE0_ALLOWED_ORIGINS", "RE0_STORAGE_MODE"):
        monkeypatch.delenv(var, raising=False)
    assert cli.main(["doctor"]) == 0
    out = capsys.readouterr().out
    assert "deployment: local" in out and "RE0_MODE 未声明" in out and "只应绑回环地址" in out

    monkeypatch.setenv("RE0_MODE", "hosted")
    assert cli.main(["doctor"]) == 2
    out = capsys.readouterr().out
    assert "deployment: hosted" in out and "拒绝启动" in out
    for required in ("RE0_SESSION_SECRET", "RE0_PUBLIC_ENTRY", "RE0_ALLOWED_ORIGINS", "RE0_STORAGE_MODE"):
        assert required in out, out

    monkeypatch.setenv("RE0_SESSION_SECRET", new_secret())
    monkeypatch.setenv("RE0_PUBLIC_ENTRY", "https://re0.example.org")
    monkeypatch.setenv("RE0_ALLOWED_ORIGINS", "https://re0.example.org")
    monkeypatch.setenv("RE0_STORAGE_MODE", "persistent")
    assert cli.main(["doctor"]) == 0
    out = capsys.readouterr().out
    assert "re0.example.org" in out and "create-user" in out


# -------------------------------------------------------------------------- migration and the CLI

def test_a_single_user_database_is_migrated_to_the_local_owner_and_backed_up(tmp_path):
    path = tmp_path / "old.sqlite3"
    import json
    import sqlite3

    old_paper = {"title": "旧论文", "authors": ["旧作者"], "year": 2024, "venue": "",
                 "abstract": "旧摘要", "doi": "10.1/old", "arxiv_id": "2401.12345v2",
                 "paper_url": "https://doi.org/10.1/old", "topics": ["旧主题", "图层分解 / 生成"],
                 "status": "reading", "notes": "保留的旧笔记", "version_label": "v2"}
    with sqlite3.connect(path) as con:
        con.executescript("""
        CREATE TABLE schema_version (version INTEGER NOT NULL);
        INSERT INTO schema_version VALUES (1);
        CREATE TABLE papers (id TEXT PRIMARY KEY,data TEXT NOT NULL,doi TEXT NOT NULL DEFAULT '',
          arxiv_base TEXT NOT NULL DEFAULT '',created_at TEXT NOT NULL,updated_at TEXT NOT NULL,
          is_demo INTEGER NOT NULL DEFAULT 0);
        CREATE UNIQUE INDEX papers_doi ON papers(doi) WHERE doi!='';
        CREATE UNIQUE INDEX papers_arxiv ON papers(arxiv_base) WHERE arxiv_base!='';
        CREATE TABLE resources (id TEXT PRIMARY KEY,paper_id TEXT NOT NULL,data TEXT NOT NULL,
          created_at TEXT NOT NULL);
        CREATE TABLE observations (id INTEGER PRIMARY KEY AUTOINCREMENT,resource_id TEXT NOT NULL,
          data TEXT NOT NULL,checked_at TEXT NOT NULL);
        CREATE TABLE topics (name TEXT PRIMARY KEY);
        INSERT INTO topics VALUES ('旧主题');
        """)
        con.execute("INSERT INTO papers VALUES (?,?,?,?,?,?,0)",
                    ("old-1", json.dumps(old_paper, ensure_ascii=False), "10.1/old",
                     "2401.12345", "2026-01-01", "2026-01-01"))
        con.execute("INSERT INTO resources VALUES (?,?,?,?)",
                    ("old-resource", "old-1", json.dumps({"url": "https://github.com/lab/old"}),
                     "2026-01-01"))
        observation = {"status": "metadata_readable", "checked_at": "2026-01-02T00:00:00+00:00",
                       "paper_version_snapshot": {"doi": "10.1/old", "arxiv_id": "2401.12345v1",
                                                  "version_label": "v1"},
                       "evidence": [{"source_url": "https://github.com/lab/old/tree/v1",
                                     "locator": "revision", "excerpt": "version one"}]}
        con.execute("INSERT INTO observations(resource_id,data,checked_at) VALUES (?,?,?)",
                    ("old-resource", json.dumps(observation), observation["checked_at"]))
    database = Database(str(path))
    from re0.service import Store
    store = Store(database)
    assert [row["title"] for row in store.list_papers(owner="local")] == ["旧论文"]
    # The direction the single user already had came across with the row. The existing paper
    # membership and its notes are not inferred from the template or overwritten by it.
    assert "旧主题" in store.topics(owner="local")
    # The upgrade copied the file before v1→v2→v3→v4 migration.
    assert list(tmp_path.glob("old.sqlite3.pre-v4-*.sqlite3")), list(tmp_path.iterdir())
    with database.connect() as con:
        assert con.execute("SELECT version FROM schema_version").fetchone()[0] == 4
        assert con.execute("SELECT owner FROM papers WHERE id='old-1'").fetchone()[0] == "local"
        assert con.execute("SELECT data FROM papers WHERE id='old-1'").fetchone()[0].find("保留的旧笔记") >= 0
        assert con.execute("SELECT COUNT(*) FROM works WHERE id='old-1'").fetchone()[0] == 1
        versions = con.execute("SELECT arxiv_id,is_current FROM paper_versions WHERE work_id='old-1'").fetchall()
        assert {(row["arxiv_id"], row["is_current"]) for row in versions} == {
            ("2401.12345v1", 0), ("2401.12345v2", 1)}
        source = con.execute("SELECT s.paper_version_id,v.arxiv_id FROM source_snapshots s "
                             "JOIN paper_versions v ON v.id=s.paper_version_id "
                             "WHERE s.observation_id=1").fetchone()
        assert tuple(source) == (next(row["id"] for row in con.execute(
            "SELECT id FROM paper_versions WHERE work_id='old-1' AND arxiv_id='2401.12345v1'")),
                                 "2401.12345v1")
        assert con.execute("SELECT COUNT(*) FROM research_relations").fetchone()[0] == 0


def test_a_newer_database_is_refused_rather_than_downgraded(tmp_path):
    path = tmp_path / "future.sqlite3"
    Database(str(path))
    with Database(str(path)).connect() as con:
        con.execute("UPDATE schema_version SET version=99")
    with pytest.raises(RuntimeError, match="Unsupported database schema"):
        Database(str(path))


def test_the_console_explains_why_local_mode_has_no_accounts(tmp_path, capsys):
    from re0.auth_cli import main

    assert main(["create-user", "--username", "alice", "--password-stdin",
                 "--db", str(tmp_path / "missing.sqlite3")]) == 2
    assert "本地模式" in capsys.readouterr().err


def test_a_session_secret_is_printed_once_and_is_long_enough(capsys):
    from re0.auth_cli import main

    assert main(["secret"]) == 0
    out, err = capsys.readouterr()
    secret = out.strip()
    assert len(secret) >= 32 and "只会打印这一次" in err
    assert from_env({**HOSTED, "RE0_SESSION_SECRET": secret}).require_startable().hosted


def test_a_paper_stored_by_one_owner_is_invisible_to_another_at_the_store_level(tmp_path):
    from re0.service import Store

    store = Store(Database(str(tmp_path / "two.sqlite3")))
    mine = store.create_paper(PaperInput(title="A 的论文", doi="10.1000/same"), owner="ws_a")
    theirs = store.create_paper(PaperInput(title="B 的论文", doi="10.1000/same"), owner="ws_b")
    assert mine["id"] != theirs["id"]
    assert [row["title"] for row in store.list_papers(owner="ws_a")] == ["A 的论文"]
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as caught:
        store.get_paper(mine["id"], owner="ws_b")
    assert caught.value.status_code == 404
    # The unique index is per owner, not absent: within one owner the same DOI is still a collision,
    # and the refusal no longer tells a second reader anything about the first.
    with pytest.raises(HTTPException) as collision:
        store.create_paper(PaperInput(title="B 的第二篇", doi="10.1000/same"), owner="ws_b")
    assert collision.value.status_code == 409
