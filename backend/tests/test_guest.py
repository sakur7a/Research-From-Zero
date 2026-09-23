"""Temporary browser guests: opt-in, independent owners, bounded retention and deletion (#18)."""
from datetime import timedelta
import socket
from threading import Event, Thread

import httpx
import pytest
from fastapi import Request
from fastapi.testclient import TestClient

from re0.auth import AccountStore
from re0.db import Database
from re0.deployment import from_env
from re0.main import create_app
from re0.workspace import managed_workspaces_root

BASE = {"RE0_MODE": "hosted", "RE0_SESSION_SECRET": "a-hosted-secret-that-is-long-enough-to-use",
        "RE0_PUBLIC_ENTRY": "https://re0.example.org", "RE0_ALLOWED_ORIGINS": "https://re0.example.org",
        "RE0_STORAGE_MODE": "ephemeral-demo", "RE0_GUEST_ACCESS": "true"}
HEADERS = {"X-Re0-Client": "web", "Content-Type": "application/json"}


def app_for(path, *, enabled=True, transport=None):
    env = {**BASE}
    if not enabled:
        env.pop("RE0_GUEST_ACCESS")
    transport = transport or httpx.MockTransport(
        lambda request: (_ for _ in ()).throw(AssertionError(f"unexpected network: {request.url}")))
    return create_app(str(path), transport,
        deployment=from_env(env).require_startable())


def guest(client):
    response = client.post("/api/auth/guest", headers=HEADERS, json={})
    assert response.status_code == 200, response.text
    return response.json()["identity"]


def test_guest_entry_is_disabled_by_default_and_cannot_be_enabled_in_local_mode(tmp_path):
    app = app_for(tmp_path / "disabled.sqlite3", enabled=False)
    with TestClient(app, headers=HEADERS, base_url="https://testserver") as client:
        assert client.post("/api/auth/guest", json={}).status_code == 404
    assert from_env({"RE0_MODE": "local", "RE0_GUEST_ACCESS": "true"}).require_startable().guest_access_enabled is False


def test_guests_get_independent_sessions_and_cannot_read_or_delete_each_others_data(tmp_path):
    app = app_for(tmp_path / "two-guests.sqlite3")
    alice = TestClient(app, headers=HEADERS, base_url="https://testserver")
    bob = TestClient(app, headers=HEADERS, base_url="https://testserver")
    a = guest(alice)
    b = guest(bob)
    assert a["kind"] == b["kind"] == "guest"
    assert a["workspace"] != b["workspace"] and a["user_id"] != b["user_id"]
    assert alice.cookies.get("re0_session") != bob.cookies.get("re0_session")
    assert next(item for item in alice.cookies.jar if item.name == "re0_session").secure
    made = alice.post("/api/papers", headers=HEADERS,
                      json={"title": "Alice guest paper", "abstract": "Alice's temporary fixture."})
    assert made.status_code == 201, made.text
    paper_id = made.json()["id"]
    assert bob.get("/api/papers", headers=HEADERS).json() == []
    assert bob.get(f"/api/papers/{paper_id}", headers=HEADERS).status_code == 404
    refused = bob.request("DELETE", "/api/auth/guest/data", headers=HEADERS, json={})
    assert refused.status_code == 200 and refused.json()["deleted"] is True
    assert alice.get("/api/papers", headers=HEADERS).json()[0]["id"] == paper_id
    assert app.state.accounts.count_accounts() == 0, "guests are not provisioned admin accounts"


def test_guest_data_delete_removes_database_rows_and_workspace_files(tmp_path):
    path = tmp_path / "guest-data.sqlite3"
    app = app_for(path)
    with TestClient(app, headers=HEADERS, base_url="https://testserver") as client:
        identity = guest(client)
        paper = client.post("/api/papers", headers=HEADERS,
                            json={"title": "Delete me", "abstract": "temporary"})
        assert paper.status_code == 201
        root = managed_workspaces_root(path, identity["workspace"])
        root.mkdir(parents=True)
        (root / "bundle.json").write_text("temporary source", encoding="utf-8")
        removed = client.request("DELETE", "/api/auth/guest/data", headers=HEADERS, json={})
        assert removed.status_code == 200, removed.text
        assert removed.json()["deleted"] is True and removed.json()["pending"] is False
        assert removed.headers.get("set-cookie", "").startswith("re0_session=")
        assert not root.exists()
        assert client.get("/api/auth/session", headers=HEADERS).json()["identity"]["authenticated"] is False
        with app.state.store.db.connect() as con:
            assert con.execute("SELECT COUNT(*) FROM papers WHERE owner=?", (identity["workspace"],)).fetchone()[0] == 0
            assert con.execute("SELECT COUNT(*) FROM auth_guest_sessions WHERE workspace=?",
                               (identity["workspace"],)).fetchone()[0] == 0


def test_guest_delete_defers_until_an_inflight_request_finishes_then_sweep_removes_data(tmp_path):
    app = app_for(tmp_path / "guest-inflight.sqlite3")
    entered, release = Event(), Event()

    def slow_guest_request(request: Request):
        entered.set()
        assert release.wait(5), "the test did not release the in-flight request"
        return {"owner": request.state.identity.workspace}

    app.add_api_route("/api/test/slow-guest", slow_guest_request, methods=["GET"])
    with TestClient(app, headers=HEADERS, base_url="https://testserver") as client:
        identity = guest(client)
        paper = client.post("/api/papers", headers=HEADERS,
                            json={"title": "Keep until request ends", "abstract": "temporary"})
        assert paper.status_code == 201
        result = {}
        request_thread = Thread(target=lambda: result.setdefault(
            "response", client.get("/api/test/slow-guest", headers=HEADERS)))
        request_thread.start()
        assert entered.wait(3), "the guest request did not reach its handler"
        deleted = client.request("DELETE", "/api/auth/guest/data", headers=HEADERS, json={})
        assert deleted.status_code == 202 and deleted.json()["pending"] is True
        assert app.state.store.list_papers(owner=identity["workspace"])
        release.set()
        request_thread.join(timeout=5)
        assert not request_thread.is_alive()
        assert result["response"].status_code == 200
        assert result["response"].json()["owner"] == identity["workspace"]

        fresh = guest(client)  # admission sweeps the revoked guest after its request count reaches zero
        assert fresh["workspace"] != identity["workspace"]
        assert app.state.store.list_papers(owner=identity["workspace"]) == []


def test_guest_sessions_expire_and_expired_owners_are_available_to_the_cleanup_worker(tmp_path):
    accounts = AccountStore(Database(str(tmp_path / "expired.sqlite3")))
    identity, token = accounts.create_guest(ttl=timedelta(seconds=-1))
    assert accounts.resolve(token) is None
    assert accounts.owner_has_live_session(identity.workspace) is False
    assert accounts.expired_guest_sessions() == [{"user_id": identity.user_id,
                                                  "workspace": identity.workspace}]


@pytest.mark.parametrize("success", [True, False])
def test_one_step_byok_connect_tests_before_it_saves_and_never_returns_the_key(tmp_path, monkeypatch, success):
    calls = []

    def network(request):
        calls.append(request)
        if success:
            message = {"role": "assistant", "content": None, "tool_calls": [{
                "id": "call-connect", "type": "function",
                "function": {"name": "connection_check", "arguments": "{}"}}]}
        else:
            message = {"role": "assistant", "content": "hello", "tool_calls": []}
        return httpx.Response(200, json={"choices": [{"message": message, "finish_reason": "tool_calls"}],
                                        "usage": {"prompt_tokens": 1, "completion_tokens": 1,
                                                  "total_tokens": 2}})

    original = socket.getaddrinfo

    def resolve(host, *args, **kwargs):
        if str(host).lower() == "api.openai.com":
            return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("93.184.216.34", 443))]
        return original(host, *args, **kwargs)

    monkeypatch.setattr(socket, "getaddrinfo", resolve)
    app = app_for(tmp_path / f"connect-{success}.sqlite3", transport=httpx.MockTransport(network))
    with TestClient(app, headers=HEADERS, base_url="https://testserver") as client:
        guest(client)
        config = {"base_url": "https://api.openai.com/v1", "model": "fixture-model",
                  "api_key": "fixture-key-never-returned", "trust_endpoint": True}
        response = client.post("/api/agent/config/connect", json=config)
        if success:
            assert response.status_code == 200, response.text
            assert response.json()["connection_test"]["tool_calling"] is True
            assert response.json()["configured"] is True
            assert "fixture-key-never-returned" not in response.text
            assert client.get("/api/agent/config").json()["configured"] is True
        else:
            assert response.status_code == 422, response.text
            assert client.get("/api/agent/config").json()["configured"] is False
        assert len(calls) == 1
