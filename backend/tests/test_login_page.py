"""The door: what the login page may assume the server says, and what it must never leak (#13).

`tests/login-core.js` tests the page's decisions against payloads written by hand. That is only worth
anything if the payloads match the server, so these tests read the real responses and pin the strings
both sides depend on. A page that renders a shape the server no longer produces is not broken — it is
silently wrong, which is worse.
"""
import re
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from re0.deployment import DeploymentError, from_env
from re0.main import create_app
from re0.quota import Quota

HOSTED = {"RE0_MODE": "hosted", "RE0_SESSION_SECRET": "a-hosted-secret-that-is-long-enough-to-use",
          "RE0_PUBLIC_ENTRY": "https://re0.example.org",
          "RE0_ALLOWED_ORIGINS": "https://re0.example.org",
          "RE0_STORAGE_MODE": "ephemeral-demo"}
WRITE = {"X-Re0-Client": "web", "Content-Type": "application/json"}
PASSWORD = "a-password-nobody-guesses"
# The two sentences `login-core.js` quotes back to the reader. They are one string in two languages on
# purpose: if the server rewords one, the page's "the server does not distinguish the four cases" claim
# has to be re-read, not quietly left pointing at a message that no longer exists.
UNIFORM_401 = "用户名或口令不正确；连续失败会暂时锁定该账户"
LOCAL_409 = "本地模式没有账户：所有数据属于本机唯一的所有者 local"


def _refuse(request):
    raise AssertionError(f"Unexpected network request: {request.url}")


def hosted(tmp_path, *, quota=None, name="login"):
    return create_app(str(tmp_path / f"{name}.sqlite3"), httpx.MockTransport(_refuse),
                      deployment=from_env(HOSTED).require_startable(), quota=quota)


def browser(app, **headers):
    """A signed-out browser. https because a hosted session cookie is Secure."""
    return TestClient(app, headers={**WRITE, **headers}, base_url="https://testserver")


def local(tmp_path, name="local-login"):
    return create_app(str(tmp_path / f"{name}.sqlite3"), httpx.MockTransport(_refuse))


# ------------------------------------------------------------------------------- serving the shell

@pytest.mark.parametrize("mode", ["hosted", "local"])
def test_the_door_is_served_in_both_modes_with_the_same_headers_as_everything_else(tmp_path, mode):
    app = hosted(tmp_path) if mode == "hosted" else local(tmp_path)
    response = browser(app).get("/login")
    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("text/html")
    # The page is the one place a reader types a secret, so the framing rules matter most here.
    csp = response.headers["content-security-policy"]
    assert "script-src 'self'" in csp and "form-action 'self'" in csp and "base-uri 'none'" in csp
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["referrer-policy"] == "no-referrer"


def test_the_page_and_the_modules_it_imports_all_travel_with_the_service(tmp_path):
    """`#14` requires static assets to be reachable in the delivered artifact, not just in a checkout.

    The login page is three files: HTML, a DOM shell, and the pure module the shell imports. A missing
    one is a page that loads and then does nothing at all.
    """
    client = browser(hosted(tmp_path))
    for asset in ("/static/login.html", "/static/login.js", "/static/login-core.js",
                  "/static/login.css", "/static/api.js", "/static/theme.js", "/static/core.js"):
        response = client.get(asset)
        assert response.status_code == 200, f"{asset} is not reachable: {response.status_code}"
        assert len(response.content) > 200, f"{asset} is suspiciously short"
    # The import graph the page depends on, resolved from the served source.
    assert "./login-core.js" in client.get("/static/login.js").text
    assert "./theme.js" in client.get("/static/login.js").text
    assert "storage-warning" in client.get("/static/login.html").text
    # `api.js` is what bounces a signed-out page to the door; if that wiring is lost, the pages go
    # quiet instead of sending the reader to the login they need.
    helper = client.get("/static/api.js").text
    assert "login-core.js" in helper and "loginRedirect" in helper and "401" in helper


def test_the_shell_carries_no_rows_and_no_identity_of_its_own(tmp_path):
    app = hosted(tmp_path)
    app.state.accounts.create_account("somebody", PASSWORD)
    html = browser(app).get("/login").text
    assert "<html" in html
    with app.state.store.db.connect() as con:
        workspace = con.execute("SELECT workspace FROM auth_accounts").fetchone()[0]
        user_id = con.execute("SELECT user_id FROM auth_accounts").fetchone()[0]
    assert workspace not in html and user_id not in html
    # The page starts with the form hidden: it knows nothing until it asks, and it says so.
    assert 'id="login-form"' in html and "hidden" in html


def test_no_page_relies_on_an_inline_script_the_apps_own_csp_blocks(tmp_path):
    """Every page carried the same theme bootstrap as an inline `<script>`.

    `script-src 'self'` without `'unsafe-inline'` blocks it, so all four pages were logging a policy
    violation on load and dark-mode readers saw the wrong theme first — invisible to the bridged
    smokes, which build a page from a string and never see the headers. The fix was an external file,
    not a weaker policy, so this checks both halves: no inline script anywhere, and the header that
    would have caught it is still being sent.
    """
    web = Path(__file__).resolve().parents[2] / "web"
    for name in ("agent.html", "index.html", "search.html", "login.html"):
        html = (web / name).read_text(encoding="utf-8")
        for tag in re.findall(r"<script\b([^>]*)>", html):
            assert "src=" in tag, f"{name} has an inline <script {tag.strip()}> the CSP forbids"
        assert "/static/theme-bootstrap.js" in html, f"{name} lost its theme bootstrap"
    client = browser(local(tmp_path, name="csp-pin"))
    health = client.get("/api/health")
    assert "script-src 'self'" in health.headers["content-security-policy"]
    assert "unsafe-inline" not in health.headers["content-security-policy"].split("script-src")[1]\
        .split(";")[0]
    bootstrap = client.get("/static/theme-bootstrap.js")
    assert bootstrap.status_code == 200 and "re0-theme" in bootstrap.text


def test_the_pages_that_bounce_link_to_the_door(tmp_path):
    client = browser(hosted(tmp_path))
    for page in ("/", "/static/search.html"):
        assert 'href="/login"' in client.get(page).text, page


# ----------------------------------------------------------- the shape the page was written against

def test_the_session_endpoint_answers_what_sessionview_reads(tmp_path):
    """Every key `sessionView()` looks for, from the real server, before anybody has logged in."""
    payload = browser(hosted(tmp_path)).get("/api/auth/session").json()
    assert payload["deployment"]["auth_required"] is True
    assert payload["deployment"]["mode"] == "hosted"
    assert payload["deployment"]["storage_mode"] == "ephemeral-demo"
    assert payload["deployment"]["session_secret_configured"] is True
    assert payload["identity"]["authenticated"] is False
    assert payload["identity"]["user_id"] == "" and payload["identity"]["workspace"] == ""
    assert isinstance(payload["accounts_provisioned"], int)
    assert "note" in payload


def test_a_signed_out_browser_can_open_the_shell_but_reads_nothing(tmp_path):
    """The 401 the reader actually sees, and the words that come with it.

    The shell is open because the page has to load before anybody can log in; the data is not. The
    detail text is what `api.js` shows while it bounces to the door, so it has to read as an
    instruction rather than a stack of bytes.
    """
    app = hosted(tmp_path)
    app.state.accounts.create_account("somebody", PASSWORD)
    client = browser(app)
    assert client.get("/").status_code == 200
    for path in ("/api/papers", "/api/agent/config", "/api/agent/runs"):
        refused = client.get(path)
        assert refused.status_code == 401, path
        assert "需要登录" in refused.json()["detail"], path
    # An id that does not exist and one that belongs to somebody else answer identically.
    assert client.get("/api/papers/nope").status_code == 401


def test_login_through_the_pages_own_contract_sets_a_cookie_the_page_cannot_read(tmp_path):
    app = hosted(tmp_path)
    app.state.accounts.create_account("reader", PASSWORD)
    client = browser(app)
    response = client.post("/api/auth/login", json={"username": "reader", "password": PASSWORD})
    assert response.status_code == 200, response.text
    cookie = response.headers["set-cookie"]
    for attribute in ("httponly", "samesite=strict", "secure"):
        assert attribute in cookie.lower(), cookie
    assert response.json()["identity"]["authenticated"] is True
    # `expires_at` is fed to `new Date()` by the page: a value it cannot parse would print "未知".
    expires = response.json()["identity"]["expires_at"]
    assert "T" in expires and (expires.endswith("Z") or "+" in expires[11:]), expires
    # The token itself is in no body, and the typed password is nowhere in what came back.
    assert PASSWORD not in response.text
    assert client.get("/api/papers").status_code == 200
    logged_out = client.post("/api/auth/logout")
    assert logged_out.status_code == 200 and logged_out.json()["revoked"] is True
    assert client.get("/api/papers").status_code == 401


def test_a_write_without_the_pages_client_header_is_refused_before_the_password_is_checked(tmp_path):
    """The page sends `X-Re0-Client: web`; a login that arrives without it is not the page.

    Checked at the door as well as everywhere else: the CSRF posture of a service is only as good as
    its weakest write, and a login that plants a session cookie is a write.
    """
    app = hosted(tmp_path)
    app.state.accounts.create_account("reader", PASSWORD)
    plain = TestClient(app, headers={"Content-Type": "application/json"}, base_url="https://testserver")
    refused = plain.post("/api/auth/login", json={"username": "reader", "password": PASSWORD})
    assert refused.status_code == 403
    assert "set-cookie" not in {key.lower() for key in refused.headers}
    # A cross-origin login is refused too, and the account is no more unlocked for having tried.
    forged = TestClient(app, headers={**WRITE, "Origin": "https://evil.example"},
                        base_url="https://testserver")
    assert forged.post("/api/auth/login",
                       json={"username": "reader", "password": PASSWORD}).status_code == 403


# ------------------------------------------------------------------ the strings both languages share

def test_the_server_still_says_the_two_sentences_the_page_quotes(tmp_path):
    app = hosted(tmp_path)
    app.state.accounts.create_account("reader", PASSWORD)
    wrong = browser(app).post("/api/auth/login", json={"username": "reader", "password": "not-it"})
    assert wrong.status_code == 401
    assert wrong.json()["detail"] == UNIFORM_401, "the page's disclaimer no longer matches the server"
    # An unknown name gets the identical bytes, so nothing in the answer distinguishes the two.
    unknown = browser(app).post("/api/auth/login", json={"username": "nobody", "password": "not-it"})
    assert unknown.status_code == 401 and unknown.json()["detail"] == UNIFORM_401
    local_client = browser(local(tmp_path, name="sentence-local"))
    local_refusal = local_client.post("/api/auth/login", json={"username": "a", "password": "b"})
    assert local_refusal.status_code == 409
    assert local_refusal.json()["detail"] == LOCAL_409, "the page's local-mode hint no longer matches"


def test_the_password_never_leaves_the_request_boundary(tmp_path):
    """Not in a body, not in an error, not in a row, not in the audit trail.

    The failure path is the interesting one: an echo of the submitted JSON would put the secret in the
    response, and the validation handler exists precisely to stop that.
    """
    app = hosted(tmp_path)
    app.state.accounts.create_account("reader", PASSWORD)
    client = browser(app)
    attempts = [client.post("/api/auth/login", json={"username": "reader", "password": "wrong-one"}),
                client.post("/api/auth/login", json={"username": "", "password": ""}),
                client.post("/api/auth/login", json={"password": PASSWORD}),
                client.post("/api/auth/login", json={"username": "reader", "password": PASSWORD,
                                                     "role": "owner"})]
    for response in attempts:
        assert PASSWORD not in response.text, response.status_code
        assert "wrong-one" not in response.text
    with app.state.store.db.connect() as con:
        dump = "\n".join(con.iterdump())
    assert PASSWORD not in dump and "wrong-one" not in dump
    # The last two never reached the password check: an empty or absent username and a body carrying
    # a field that decides nothing are validation failures, and `Login` forbids extras precisely
    # because a silently ignored `role` is a caller arguing with the shape of the request.
    assert [response.status_code for response in attempts] == [401, 422, 422, 422], \
        [response.text for response in attempts]
    forbidden = attempts[3].json()["detail"]
    assert [item["loc"][-1] for item in forbidden] == ["role"]
    assert all(item["msg"] == "输入格式或取值不符合要求" for item in forbidden), forbidden


# ------------------------------------------------------------------------------- and the ceiling on it

def test_a_flooded_login_is_a_ceiling_not_a_lockout(tmp_path):
    """A 429 at the door must read as "wait", because the wrong reading is "keep trying".

    Retrying against a ceiling both wastes the reader's attempts and looks like the account is broken.
    Logged-out traffic has no account to blame, so the message says 未登录 rather than inventing one.
    """
    app = hosted(tmp_path, quota=Quota(requests_per_minute=4, site_requests_per_minute=500,
                                       tasks_per_hour=500), name="flood")
    app.state.accounts.create_account("reader", PASSWORD)
    client = browser(app)
    codes = [client.post("/api/auth/login",
                         json={"username": "reader", "password": "not-it"}).status_code
             for _ in range(6)]
    # Four attempts reach the password check, then the window closes. Nothing about the sixth is an
    # auth failure, which is why the counts have to be read in order rather than as a ratio.
    assert codes == [401, 401, 401, 401, 429, 429], codes
    refused = client.post("/api/auth/login", json={"username": "reader", "password": "not-it"})
    assert refused.status_code == 429
    detail = refused.json()["detail"]
    assert "未登录请求配额已用尽" in detail, detail
    assert "没有产生模型调用或费用" in detail
    assert refused.headers["retry-after"]
    # The ceiling fell on the anonymous bucket, not on the account. Read the failure counter *before*
    # logging in successfully — a success resets it, and asserting afterwards would prove nothing.
    from re0.auth import AccountStore
    with app.state.store.db.connect() as con:
        failures = con.execute("SELECT failed_count FROM auth_accounts").fetchone()[0]
    assert failures == 4, f"refused requests were counted as failed logins ({failures})"
    store = AccountStore(app.state.store.db)
    assert store.verify("reader", PASSWORD), "the reader's own account was punished for a flood"
    with app.state.store.db.connect() as con:
        assert con.execute("SELECT failed_count FROM auth_accounts").fetchone()[0] == 0
