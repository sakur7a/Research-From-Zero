"""A real Chromium browser, real navigation, real cookies — and still nothing but this process.

The other three smokes build a page with `set_content` and bridge `window.fetch` to an in-process
TestClient. That is the right shape for testing a view, and the wrong shape for testing a **login
page**: the things that matter here are the ones a synthetic page does not have — an origin, a query
string, a response header that sets a cookie, a content-security-policy that decides whether the
page's own inline script runs at all, and a navigation that either lands or does not.

So this one routes `https://re0.test/**` into the same TestClient, and lets the browser be a browser:
`page.goto` a real URL with a real `?next=`, read the real `Set-Cookie`, let the browser hold the
session in its own jar, and let it navigate. Nothing reaches outside this machine: the only origin the
browser is allowed to ask for is the fake one, and every request to it is answered from the process.

What this proves: the mode probe decides what the page offers; a wrong password is one sentence that
does not say which of four cases it was; the typed secret is gone from the field after every attempt;
the session cookie is HttpOnly (page script cannot read it) and survives into the next request; a
`?next=` that is not a path on this origin cannot send the reader anywhere; logout ends the session
server-side, not by clearing the tab. What it does not prove: TLS, a reverse proxy, or a second human
on a second device — that stays in `#14`'s deployment stage.

Requires optional `playwright` and a Chromium executable (CHROMIUM_PATH).
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import tempfile
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

import httpx
from fastapi.testclient import TestClient
from playwright.sync_api import sync_playwright
from re0.deployment import from_env
from re0.main import create_app

ORIGIN = "https://re0.test"
HOSTED = {"RE0_MODE": "hosted", "RE0_SESSION_SECRET": "a-long-enough-secret-for-this-smoke-run",
          "RE0_PUBLIC_ENTRY": ORIGIN, "RE0_ALLOWED_ORIGINS": ORIGIN}
PASSWORD = "a-password-nobody-guesses"
# Headers that describe the bytes httpx already decoded. Forwarding them over a fulfilled response
# would tell the browser to decode a body that is not encoded. `set-cookie` is deliberately *not* in
# this list: the cookie has to reach the browser, because the browser is the thing holding the session.
DROPPED = {"content-encoding", "content-length", "transfer-encoding", "connection"}


def login_failure_detail(client) -> str:
    response = client.post("/api/auth/login", headers={"X-Re0-Client": "web",
                                                       "Content-Type": "application/json"},
                           json={"username": "reader", "password": "not-the-password"})
    return response.json()["detail"]


def run(output_dir: Path):
    output_dir.mkdir(parents=True, exist_ok=True)
    errors, completed, served = [], [], []
    # Chromium logs any 401 it fetches as a console error, and this test asks for a few on purpose.
    # So refusals are checked where they are asserted (below), and a 401 that arrives while nobody was
    # expecting one is collected separately rather than drowned in the console noise.
    expecting_refusal = []
    unexpected_refusals = []

    def no_network(request):
        raise AssertionError(f"Offline browser test must not contact external providers: {request.url}")

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp, \
            sync_playwright() as engine:
        launch = {"headless": True}
        if os.getenv("CHROMIUM_PATH"):
            launch["executable_path"] = os.getenv("CHROMIUM_PATH")
        browser = engine.chromium.launch(**launch)

        def session(path: Path, **kwargs):
            """One app, one browser context: the context owns the cookies, exactly as it should."""
            app = create_app(str(path), httpx.MockTransport(no_network), **kwargs)
            client = TestClient(app)
            client.__enter__()

            def handle(route):
                request = route.request
                target = urlsplit(request.url)
                forwarded = {key: value for key, value in request.headers.items()
                             if key.lower() not in ("host", "content-length", "accept-encoding")}
                upstream = client.request(request.method, target.path
                                          + (f"?{target.query}" if target.query else ""),
                                          headers=forwarded,
                                          content=request.post_data_buffer or None)
                headers = {key: value for key, value in upstream.headers.items()
                           if key.lower() not in DROPPED}
                served.append((request.method, target.path, upstream.status_code))
                if upstream.status_code == 401 and not expecting_refusal:
                    unexpected_refusals.append((request.method, target.path))
                return route.fulfill(status=upstream.status_code, body=upstream.content,
                                     headers=headers)

            context = browser.new_context(viewport={"width": 1280, "height": 900})
            page = context.new_page()
            page.on("pageerror", lambda error: errors.append(f"pageerror: {error}"))
            page.on("console", lambda message: errors.append(f"console-{message.type}: {message.text}")
                    if message.type in ("error", "warning")
                    and not message.text.startswith("Failed to load resource") else None)
            page.route(f"{ORIGIN}/**", handle)
            return app, client, context, page

        # ---------------------------------------------------------------- 1. local mode says so plainly
        local_app, _, _, page = session(Path(temp) / "local.sqlite3")
        page.goto(f"{ORIGIN}/login?next=%2Flibrary")
        page.wait_for_selector("#no-login:not([hidden])")
        text = page.inner_text("#door")
        assert "这台服务不需要登录" in text, text
        assert page.is_hidden("#login-form"), "local mode offered a form that cannot work"
        assert page.is_hidden("#signed-in"), "the local page claimed a session it cannot have"
        page.screenshot(path=str(output_dir / "01-local-mode.png"), full_page=True)
        assert not errors, errors
        completed.append("local_mode_refuses_to_pretend_there_is_a_login")
        page.close()
        local_app.state.agent.close()

        # ------------------------------------------------- 2. hosted, no accounts: the form and its hint
        first = Path(temp) / "hosted.sqlite3"
        app, client, context, page = session(first, deployment=from_env(HOSTED).require_startable())
        page.goto(f"{ORIGIN}/login")
        page.wait_for_selector("#login-form:not([hidden])")
        assert page.inner_text("#door-alt").strip(), "the empty-account hint did not render"
        assert "auth create-user" in page.inner_text("#door-alt")
        assert page.is_visible("#username") and page.is_visible("#password")
        assert page.evaluate("document.activeElement.id") == "username", "keyboard users land nowhere"
        assert "auth_required" not in page.inner_text("#door").lower()
        page.screenshot(path=str(output_dir / "02-hosted-no-accounts.png"), full_page=True)
        completed.append("hosted_without_accounts_says_who_creates_them")

        # ------------------------------------------------------- 3. a wrong password stays one sentence
        app.state.accounts.create_account("reader", PASSWORD)
        uniform = login_failure_detail(client)
        page.fill("#username", "reader")
        page.fill("#password", "not-the-password")
        expecting_refusal.append(True)   # this one is the point of the step
        page.click("#login-submit")
        page.wait_for_selector(".notice.error")
        expecting_refusal.clear()
        shown = page.inner_text(".notice.error")
        assert uniform in shown, (shown, uniform)
        # The page named the four cases *as an enumeration*, which is the only framing that keeps the
        # server's deliberate ambiguity: a diagnosis of this account would be an oracle the API refuses
        # to be, and one reworded sentence is enough to build it.
        assert "四种情况" in shown and "服务器不区分" in shown, shown
        for case in ("口令错", "账户不存在", "已停用", "正在锁定"):
            assert case in shown, shown
        assert "你的账户" not in shown and "这个账户" not in shown, shown
        assert page.input_value("#password") == "", "the typed password stayed in the field"
        assert page.input_value("#username") == "reader", "the name is worth keeping; the secret is not"
        page.screenshot(path=str(output_dir / "03-uniform-failure.png"), full_page=True)
        completed.append("one_sentence_for_four_cases_and_no_secret_left_behind")

        # 4. The failure is not written anywhere a later reader could find it.
        stored = page.evaluate("() => ({local: Object.keys(localStorage), session:"
                               " Object.keys(sessionStorage), cookie: document.cookie})")
        assert stored["session"] == [], stored
        assert [key for key in stored["local"] if not key.startswith("re0-")] == [], stored
        assert "re0_session=" not in stored["cookie"], "the session cookie is readable by page script"
        assert PASSWORD not in json.dumps(stored)
        assert "not-the-password" not in json.dumps(stored)
        completed.append("no_password_and_no_session_token_anywhere_the_page_can_read")

        # --------------------------------- 5. the real success path, including the navigation it makes
        page.goto(f"{ORIGIN}/login?next=%2Flibrary")
        page.wait_for_selector("#login-form:not([hidden])")
        page.fill("#username", "reader")
        page.fill("#password", PASSWORD)
        page.click("#login-submit")
        page.wait_for_url(f"{ORIGIN}/library", timeout=15000)
        assert page.url == f"{ORIGIN}/library", page.url
        completed.append("a_real_navigation_lands_where_the_query_string_pointed")

        # 6. The session the browser now holds belongs to that account, and to nobody else. The check
        # is a fetch *from the page*, because the cookie lives in the browser: the in-process client
        # has its own jar and was never given this session, and asserting against it would be asserting
        # that a stranger is not signed in.
        papers = page.evaluate("async () => (await fetch('/api/papers', {headers:"
                               "{'X-Re0-Client':'web','Content-Type':'application/json'}})).status")
        assert papers == 200, f"the browser's own session was refused: {papers}"
        foreign = TestClient(app).get("/api/papers", headers={"X-Re0-Client": "web",
                                                             "Content-Type": "application/json"})
        assert foreign.status_code == 401, "a jar that never logged in got through"
        page.goto(f"{ORIGIN}/login")
        page.wait_for_selector("#signed-in:not([hidden])")
        door = page.inner_text("#door")
        assert "usr_" in door and "ws_" in door, door
        assert page.inner_text("#when-expires").strip() not in ("", "未知"), "expiry was unparsable"
        page.screenshot(path=str(output_dir / "04-signed-in.png"), full_page=True)
        completed.append("the_signed_in_panel_shows_this_session_and_when_it_ends")

        # 7. A `next` that is not a path on this origin is refused, in the page's own words.
        page.goto(f"{ORIGIN}/login?next=%2F%2Fevil.example")
        page.wait_for_selector("#signed-in:not([hidden])")
        link = page.get_attribute("#continue-link", "href")
        assert link == "/", link
        resolved = page.evaluate("() => document.getElementById('continue-link').href")
        assert resolved.startswith(f"{ORIGIN}/"), resolved
        assert "evil.example" not in resolved, "the continue link could leave the origin"
        completed.append("a_scheme_relative_next_cannot_send_the_reader_elsewhere")

        # 8. Logout is a server-side act, and the panel returns to the door.
        page.click("#logout")
        page.wait_for_selector("#login-form:not([hidden])")
        assert "已退出" in page.inner_text(".notice.ok"), page.inner_text("#door")
        expecting_refusal.append(True)   # a revoked session must be refused; that is the assertion
        after = page.evaluate("async () => (await fetch('/api/papers', {headers:"
                              "{'X-Re0-Client':'web','Content-Type':'application/json'}})).status")
        expecting_refusal.clear()
        assert after == 401, f"the revoked session still worked server-side: {after}"
        page.screenshot(path=str(output_dir / "05-logged-out.png"), full_page=True)
        completed.append("logout_ends_the_session_on_the_server_not_in_the_tab")

        # 9. Narrow screen: the door stays inside it.
        page.set_viewport_size({"width": 390, "height": 780})
        page.goto(f"{ORIGIN}/login")
        page.wait_for_selector("#login-form:not([hidden])")
        overflow = page.evaluate("() => document.documentElement.scrollWidth"
                                 " - document.documentElement.clientWidth")
        assert overflow <= 1, f"the login page overflows a phone by {overflow}px"
        page.screenshot(path=str(output_dir / "06-narrow.png"), full_page=True)
        completed.append("the_door_fits_a_phone")

        assert not errors, errors
        context.close()
        app.state.agent.close()

    assert not unexpected_refusals, f"the browser was refused where it should not have been: {unexpected_refusals}"
    report = {"steps": completed, "served": [list(item) for item in served],
              "browser_errors": errors, "requests": len(served),
              "deliberate_401_count": sum(1 for item in served if item[2] == 401)}
    (output_dir / "login-report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2),
                                                  encoding="utf-8")
    print(json.dumps({"steps": completed, "browser_errors": errors,
                      "requests": len(served)}, ensure_ascii=False, indent=2))
    return 0 if not errors and len(completed) == 9 else 1


if __name__ == "__main__":
    sys.exit(run(Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "test-results" / "login-browser"))
