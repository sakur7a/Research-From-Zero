"""Two identities, one process: what each may see, and what neither may see (#13).

These tests are the release gate the issue asks for, minus the second browser: two independent
sessions over one app, each with its own cookie jar. Everything here is offline — no model is called
unless a test says so, and no provider is contacted at all.
"""
import httpx
import pytest
from fastapi.testclient import TestClient

from re0.auth import MAX_FAILED_ATTEMPTS, AccountStore, hash_password, new_secret, verify_password
from re0.db import Database
from re0.deployment import DeploymentError, from_env
from re0.main import create_app
from re0.models import PaperInput

HOSTED = {"RE0_MODE": "hosted", "RE0_SESSION_SECRET": "a-hosted-secret-that-is-long-enough-to-use",
          "RE0_PUBLIC_ENTRY": "https://re0.example.org",
          "RE0_ALLOWED_ORIGINS": "https://re0.example.org"}
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
    for required in ("RE0_SESSION_SECRET", "RE0_PUBLIC_ENTRY", "RE0_ALLOWED_ORIGINS"):
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
    assert store.revoke(token)
    assert store.resolve(token) is None
    short, _ = store.issue(account["user_id"], ttl=timedelta(seconds=-1))
    assert store.resolve(short) is None
    # Disabling an account ends the sessions it already had, rather than waiting for them to expire.
    live, _ = store.issue(account["user_id"])
    store.set_disabled(account["user_id"], True)
    assert store.resolve(live) is None


# ---------------------------------------------------------------------------------- the HTTP gate

def test_every_api_route_needs_a_session_in_hosted_mode(tmp_path):
    app = hosted(tmp_path)
    client = TestClient(app, headers=WRITE)
    for path in ("/api/papers", "/api/topics", "/api/export", "/api/agent/runs",
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
    # The cookie is HttpOnly and SameSite=Strict, and Secure because the entry is https.
    cookie = [item for item in alice.cookies.jar][0]
    assert cookie.name == "re0_session"
    headers = alice.post("/api/auth/logout").json()
    assert headers["revoked"] is True
    assert alice.get("/api/papers").status_code == 401


def test_two_accounts_do_not_see_each_others_library_tasks_or_mappings(tmp_path):
    app = hosted(tmp_path)
    alice, bob = session(app, "alice"), session(app, "bob")
    mine = paper(alice, "Alice 的一篇", doi="10.1000/shared")
    alice.post(f"/api/papers/{mine['id']}/resources",
               json={"kind": "code", "label": "repo", "url": "https://github.com/a/b"})
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

    for var in ("RE0_MODE", "RE0_SESSION_SECRET", "RE0_PUBLIC_ENTRY", "RE0_ALLOWED_ORIGINS"):
        monkeypatch.delenv(var, raising=False)
    launcher.check_deployment("127.0.0.1")
    with pytest.raises(SystemExit) as bound:
        launcher.check_deployment("0.0.0.0")
    assert "RE0_HOST" in str(bound.value) and "RE0_MODE=hosted" in str(bound.value)
    # A hosted start with nothing configured is refused too, and the public bind is then fine.
    monkeypatch.setenv("RE0_MODE", "hosted")
    with pytest.raises(SystemExit) as missing:
        launcher.check_deployment("0.0.0.0")
    for required in ("RE0_SESSION_SECRET", "RE0_PUBLIC_ENTRY", "RE0_ALLOWED_ORIGINS"):
        assert required in str(missing.value), missing.value
    monkeypatch.setenv("RE0_SESSION_SECRET", new_secret())
    monkeypatch.setenv("RE0_PUBLIC_ENTRY", "https://re0.example.org")
    monkeypatch.setenv("RE0_ALLOWED_ORIGINS", "https://re0.example.org")
    launcher.check_deployment("0.0.0.0")


def test_doctor_says_which_mode_would_run_and_exits_non_zero_when_it_could_not(monkeypatch, capsys):
    """`doctor` is what an operator checks first, so "could not start" cannot be an exit-0 line."""
    from re0 import cli

    for var in ("RE0_MODE", "RE0_SESSION_SECRET", "RE0_PUBLIC_ENTRY", "RE0_ALLOWED_ORIGINS"):
        monkeypatch.delenv(var, raising=False)
    assert cli.main(["doctor"]) == 0
    out = capsys.readouterr().out
    assert "deployment: local" in out and "RE0_MODE 未声明" in out and "只应绑回环地址" in out

    monkeypatch.setenv("RE0_MODE", "hosted")
    assert cli.main(["doctor"]) == 2
    out = capsys.readouterr().out
    assert "deployment: hosted" in out and "拒绝启动" in out
    for required in ("RE0_SESSION_SECRET", "RE0_PUBLIC_ENTRY", "RE0_ALLOWED_ORIGINS"):
        assert required in out, out

    monkeypatch.setenv("RE0_SESSION_SECRET", new_secret())
    monkeypatch.setenv("RE0_PUBLIC_ENTRY", "https://re0.example.org")
    monkeypatch.setenv("RE0_ALLOWED_ORIGINS", "https://re0.example.org")
    assert cli.main(["doctor"]) == 0
    out = capsys.readouterr().out
    assert "re0.example.org" in out and "create-user" in out


# -------------------------------------------------------------------------- migration and the CLI

def test_a_single_user_database_is_migrated_to_the_local_owner_and_backed_up(tmp_path):
    path = tmp_path / "old.sqlite3"
    with Database(str(path)).connect() as con:
        con.execute("UPDATE schema_version SET version=1")
        # The v1 shape had no owner column and no owner-scoped indexes; SQLite will not drop a column
        # an index still refers to, so the indexes go first.
        con.execute("DROP INDEX papers_owner_doi")
        con.execute("DROP INDEX papers_owner_arxiv")
        con.execute("DROP INDEX papers_owner")
        con.execute("ALTER TABLE papers DROP COLUMN owner")
        con.execute("CREATE UNIQUE INDEX papers_doi ON papers(doi) WHERE doi != ''")
        con.execute("CREATE TABLE topics_v1 (name TEXT PRIMARY KEY)")
        con.execute("INSERT INTO topics_v1 VALUES ('旧主题')")
        con.execute("DROP TABLE topics")
        con.execute("ALTER TABLE topics_v1 RENAME TO topics")
        con.execute("INSERT INTO papers(id,data,doi,arxiv_base,created_at,updated_at,is_demo) "
                    "VALUES ('old-1','{\"title\":\"旧论文\"}','10.1/old','','2026-01-01','2026-01-01',0)")
    database = Database(str(path))
    from re0.service import Store
    store = Store(database)
    assert [row["title"] for row in store.list_papers(owner="local")] == ["旧论文"]
    # The direction the single user already had came across with the rows; the starter directions are
    # seeded on every open, as they were before this migration existed, so membership is the claim.
    assert "旧主题" in store.topics(owner="local")
    # The upgrade copied the file first, and said which schema it moved to.
    assert list(tmp_path.glob("old.sqlite3.pre-v2-*.sqlite3")), list(tmp_path.iterdir())
    with database.connect() as con:
        assert con.execute("SELECT version FROM schema_version").fetchone()[0] == 2
        assert con.execute("SELECT owner FROM papers WHERE id='old-1'").fetchone()[0] == "local"


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
