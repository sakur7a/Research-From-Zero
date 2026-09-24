"""Chromium over real loopback HTTP for the Agent → matrix → follow-up path.

Unlike `agent_browser_smoke.py`, this does not bridge browser fetches through TestClient. Chromium
talks over TCP to a separate FastAPI process. Model and research-provider traffic is still fixture
backed inside that process; this is not a real-model, TLS, proxy or public-host acceptance test.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import secrets
import socket
import subprocess
import sys
import tempfile
import time

import httpx
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError, sync_playwright

ROOT = Path(__file__).resolve().parents[1]
SERVER = ROOT / "scripts" / "agent_real_http_smoke_server.py"
sys.path.insert(0, str(ROOT / "backend"))
sys.path.insert(0, str(ROOT / "backend" / "tests"))
from test_agent_matrix import CONFIG, GOAL  # noqa: E402


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def run(output: Path) -> dict:
    output.parent.mkdir(parents=True, exist_ok=True)
    page_errors: list[str] = []
    non_local_requests: list[str] = []
    browser_requests: list[str] = []
    completed: list[str] = []

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp:
        directory = Path(temp)
        port = free_port()
        certificate = os.getenv("AGENT_SMOKE_TLS_CERT")
        private_key = os.getenv("AGENT_SMOKE_TLS_KEY")
        if bool(certificate) != bool(private_key):
            raise ValueError("AGENT_SMOKE_TLS_CERT and AGENT_SMOKE_TLS_KEY must be set together")
        hosted = bool(certificate)
        scheme, hostname = ("https", "re0.test") if hosted else ("http", "127.0.0.1")
        base = f"{scheme}://{hostname}:{port}"
        database = directory / "agent-real-http.sqlite3"
        log_path = directory / "server.log"
        env = {key: value for key, value in os.environ.items()
               if not key.startswith("RE0_") and key not in {
                   "TAVILY_API_KEY", "GITHUB_TOKEN", "SEMANTIC_SCHOLAR_API_KEY",
                   "SEMANTICSCHOLAR_API_KEY", "OPENALEX_MAILTO", "OPENREVIEW_TOKEN",
               }}
        env.update(RE0_DB=str(database))
        if hosted:
            env.update(RE0_MODE="hosted", RE0_SESSION_SECRET=secrets.token_urlsafe(48),
                       RE0_PUBLIC_ENTRY=base, RE0_ALLOWED_ORIGINS=base,
                       RE0_ALLOWED_HOSTS=hostname, RE0_STORAGE_MODE="ephemeral-demo",
                       RE0_GUEST_ACCESS="true")
        else:
            env.update(RE0_MODE="local")
        if hosted:
            # This private test origin passes the app's HTTPS/non-loopback configuration checks,
            # while the smoke process and Chromium deliberately route it only to loopback.
            original_getaddrinfo = socket.getaddrinfo

            def resolve_test_origin(host, *args, **kwargs):
                if str(host).lower() == "re0.test":
                    return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "",
                             ("127.0.0.1", port))]
                return original_getaddrinfo(host, *args, **kwargs)

            socket.getaddrinfo = resolve_test_origin
        command = [sys.executable, str(SERVER), "--port", str(port), "--database", str(database)]
        if hosted:
            command.extend(["--certificate", certificate, "--private-key", private_key])

        with log_path.open("w", encoding="utf-8") as log:
            process = subprocess.Popen(
                command, cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT)
            try:
                with httpx.Client(base_url=base, timeout=3, trust_env=False,
                                  verify=not hosted) as client:
                    def session_request(browser_context, path: str, *, method="GET", payload=None,
                                        headers=None):
                        request_headers = dict(headers or {})
                        if hosted:
                            cookie = next((item for item in browser_context.cookies(base)
                                           if item["name"] == "re0_session"), None)
                            if cookie is None:
                                raise AssertionError("browser session cookie was not retained")
                            request_headers["Cookie"] = f"re0_session={cookie['value']}"
                        if method != "GET":
                            request_headers.setdefault("X-Re0-Client", "web")
                            request_headers.setdefault("Origin", base)
                        return client.request(method, base + path, json=payload,
                                              headers=request_headers)

                    ready_by = time.monotonic() + 30
                    while True:
                        try:
                            ready = client.get("/api/health")
                            if ready.status_code == 200:
                                break
                        except httpx.HTTPError:
                            pass
                        if process.poll() is not None or time.monotonic() >= ready_by:
                            detail = log_path.read_text(encoding="utf-8", errors="replace")[-2000:]
                            raise RuntimeError("Agent HTTP smoke server did not start:\n" + detail)
                        time.sleep(0.1)

                    health = ready.json()
                    assert health["mode"] == ("hosted" if hosted else "local")
                    assert not health["llm_enabled"]
                    anonymous = client.get("/api/auth/session").json()
                    if hosted:
                        assert anonymous["identity"]["kind"] == "anonymous"
                        assert anonymous["deployment"]["guest_access_enabled"] is True
                    else:
                        assert anonymous["identity"]["kind"] == "local"
                    completed.append("real_process_identity_and_unconfigured_state")

                    with sync_playwright() as engine:
                        launch = {"headless": True}
                        if hosted:
                            launch["args"] = ["--host-resolver-rules=MAP re0.test 127.0.0.1"]
                        if os.getenv("CHROMIUM_PATH"):
                            launch["executable_path"] = os.environ["CHROMIUM_PATH"]
                        browser = engine.chromium.launch(**launch)
                        context = browser.new_context(viewport={"width": 1440, "height": 1000},
                                                      accept_downloads=True,
                                                      ignore_https_errors=hosted)

                        def guard_request(route):
                            url = route.request.url
                            if url.startswith(base + "/"):
                                browser_requests.append(url)
                                route.continue_()
                            else:
                                non_local_requests.append(url)
                                route.abort()

                        def browser_session_probe(probe_page, run_id: str, phase: str) -> dict:
                            probe_page.goto(
                                base + "/__smoke__/session-probe?run=" + run_id + "&phase=" + phase,
                                wait_until="networkidle")
                            probe_page.locator('#result[data-done="true"]').wait_for(
                                state="visible", timeout=10000)
                            return {item["name"]: item for item in
                                    json.loads(probe_page.locator("#result").inner_text())}

                        context.route("**/*", guard_request)
                        page = context.new_page()
                        page.set_default_timeout(15000)
                        page.on("pageerror", lambda error: page_errors.append(str(error)))
                        page.goto(base + "/", wait_until="networkidle")
                        if hosted:
                            page.get_by_role("button", name="创建临时会话并继续").click()
                            page.locator("#identity-label").wait_for(state="visible", timeout=15000)
                            session_cookie = next(cookie for cookie in context.cookies(base)
                                                  if cookie["name"] == "re0_session")
                            assert session_cookie["secure"] and session_cookie["httpOnly"]
                            assert session_cookie["sameSite"] == "Strict"
                            session_identity = session_request(
                                context, "/api/auth/session").json()["identity"]
                            assert session_identity["kind"] == "guest"
                            completed.append("real_https_guest_identity_secure_cookie_and_session")
                        else:
                            assert "LOCAL WORKSPACE" in page.locator("#identity-label").inner_text()
                        page.locator("#identity-label").wait_for()
                        assert page.locator("#task-form [type=submit]").is_disabled()
                        completed.append("real_browser_load_over_tcp_and_identity_render")

                        # Use the ordinary settings dialog and the ordinary connection test. The
                        # sentinel key is consumed only by the in-process MockTransport fixture.
                        page.locator(".setup-note [data-action=settings]").click()
                        page.locator("#settings").wait_for()
                        page.locator('[name="base_url"]').fill(CONFIG["base_url"])
                        page.locator('[name="model"]').fill(CONFIG["model"])
                        page.locator('[name="api_key"]').fill(CONFIG["api_key"])
                        page.locator('[name="trust_endpoint"]').check()
                        page.locator("#model-form [type=submit]").click()
                        page.locator("#settings").wait_for(state="hidden")
                        saved_config = session_request(context, "/api/agent/config")
                        assert saved_config.status_code == 200 and saved_config.json()["configured"] is True
                        assert CONFIG["api_key"] not in saved_config.text
                        completed.append("real_http_model_connection_test_and_key_redaction")

                        other_context = None
                        if hosted:
                            other_context = browser.new_context(
                                viewport={"width": 1280, "height": 900}, ignore_https_errors=True)
                            other_context.route("**/*", guard_request)
                            other_page = other_context.new_page()
                            other_page.on("pageerror", lambda error: page_errors.append(str(error)))
                            other_page.goto(base + "/", wait_until="networkidle")
                            other_page.get_by_role("button", name="创建临时会话并继续").click()
                            other_page.locator("#identity-label").wait_for(state="visible")
                            guest_b_cookie = next(cookie for cookie in other_context.cookies(base)
                                                  if cookie["name"] == "re0_session")
                            assert guest_b_cookie["secure"] and guest_b_cookie["httpOnly"]
                            assert guest_b_cookie["value"] != session_cookie["value"]
                            initial_probe = browser_session_probe(other_page, "not-a-real-run", "before")
                            assert initial_probe["config"]["status"] == 200
                            assert initial_probe["config"]["configured"] is False
                            assert initial_probe["runs"]["count"] == 0
                            completed.append("real_https_second_guest_gets_an_independent_session")

                        page.locator("#goal").fill(GOAL["goal"])
                        page.locator('[name="consent_to_send"]').check()
                        page.locator("#task-form [type=submit]").click()
                        runs = []
                        started_by = time.monotonic() + 10
                        while time.monotonic() < started_by:
                            runs = session_request(context, "/api/agent/runs").json()
                            if runs:
                                break
                            time.sleep(0.05)
                        assert runs, "the browser did not create a task"
                        first = next(row for row in runs if row["turn"] == 1)
                        assert first["status"] in {"queued", "running"}, first
                        if hosted:
                            active_probe = browser_session_probe(other_page, first["id"], "active")
                            assert active_probe["runs"]["count"] == 0
                            assert active_probe["detail"]["status"] == 404
                            assert active_probe["export"]["status"] == 404
                            assert active_probe["cancel"]["status"] in {404, 409}
                            still_running = session_request(
                                context, f"/api/agent/runs/{first['id']}").json()
                            assert still_running["status"] in {"queued", "running"}, still_running["status"]
                            completed.append("real_https_other_guest_cannot_cancel_running_work")
                        page.locator(".status.completed").wait_for(state="visible", timeout=60000)
                        page.locator('[data-tab="matrix"]').wait_for()
                        page.locator('[data-tab="matrix"]').click()
                        page.locator(".matrix-card").first.wait_for()
                        assert page.locator(".matrix-card").count() == 4
                        assert page.locator('input[name="matrix-selection"]').count() == 3
                        completed.append("real_http_task_produces_evidence_linked_resource_matrix")

                        # Follow a matrix citation to the actual stored source excerpt before
                        # submitting a second turn.
                        page.locator('[data-tab="report"]').click()
                        page.locator(".citations button").first.click()
                        page.locator(".evidence-card").first.wait_for()
                        page.locator(".evidence-card details summary").first.click()
                        source_text = page.locator(".evidence-card pre").first.inner_text()
                        assert "Fixture" in source_text or "LayerKit" in source_text
                        completed.append("real_http_report_citation_opens_stored_source")

                        page.locator(".followup summary").click()
                        page.locator('#followup-form [name="goal"]').fill(
                            "保留一篇候选并继续说明资源核验尚未证明作者归属")
                        page.locator('#followup-form [name="research_scope"]').select_option("expanded")
                        page.locator('#followup-form .reuse input[type="checkbox"]').first.check()
                        page.locator('#followup-form [name="authorize"]').check()
                        page.locator('#followup-form [type="submit"]').click()
                        completed_by = time.monotonic() + 60
                        while time.monotonic() < completed_by:
                            runs = session_request(context, "/api/agent/runs").json()
                            candidate = next((row for row in runs if row["turn"] == 2), None)
                            if candidate and candidate["status"] == "completed":
                                break
                            time.sleep(0.1)
                        page.locator(".run-controls span").filter(
                            has_text="第 2 轮 · 追问").wait_for(state="visible", timeout=15000)
                        page.locator(".status.completed").wait_for(state="visible", timeout=15000)
                        followup = next(row for row in runs if row["turn"] == 2)
                        assert followup["status"] == "completed"
                        assert followup["conversation_id"] == runs[0]["conversation_id"]
                        followup_detail = session_request(
                            context, f"/api/agent/runs/{followup['id']}").json()
                        assert followup_detail["origin"]["permissions"]["reuse_count"] == 1
                        assert any(item.get("reused_from") for item in followup_detail["evidence"])
                        completed.append("real_http_followup_reuses_conversation_and_completes")
                        if hosted:
                            terminal_probe = browser_session_probe(other_page, first["id"], "completed")
                            assert terminal_probe["config"]["configured"] is False
                            assert terminal_probe["runs"]["count"] == 0
                            assert terminal_probe["detail"]["status"] == 404
                            assert terminal_probe["export"]["status"] == 404
                            assert len(session_request(context, "/api/agent/runs").json()) == 2
                            completed.append("real_https_guest_read_and_export_are_owner_scoped")
                            other_context.close()

                        # Return to turn one, then preview and explicitly approve the matrix
                        # through the same page. Preview must not write; confirmation must.
                        first_history_item = page.locator(
                            f'.history-item[data-run="{first["id"]}"]')
                        first_history_item.click()
                        page.locator(".run-heading h1").filter(
                            has_text=GOAL["goal"]).wait_for(state="visible")
                        page.locator(".resource-matrix").wait_for(state="visible")
                        page.locator('[data-tab="matrix"]').click()
                        selections = page.locator('input[name="matrix-selection"]')
                        assert selections.count() == 3
                        page.set_viewport_size({"width": 390, "height": 844})
                        matrix_box = page.locator(".resource-matrix").bounding_box()
                        assert matrix_box and matrix_box["x"] >= 0 \
                            and matrix_box["x"] + matrix_box["width"] <= 390, matrix_box
                        selections.first.focus()
                        page.keyboard.press("Space")
                        assert selections.first.is_checked(), "matrix selection must work from the keyboard"
                        for index in range(1, selections.count()):
                            selections.nth(index).check()
                        page.locator('[data-action="matrix-preview"]').click()
                        try:
                            page.locator("#matrix-import-preview strong").wait_for(state="visible")
                        except PlaywrightTimeoutError as exc:
                            raise AssertionError(
                                "matrix preview failed after follow-up navigation: "
                                f"run={first['id']}, preview={page.locator('#matrix-import-preview').inner_text()}, "
                                f"notice={page.locator('#notice').inner_text()}, "
                                f"selected={selections.count()}") from exc
                        preview_text = page.locator("#matrix-import-preview").inner_text()
                        assert "预览：2 组来源数据" in preview_text, preview_text
                        assert session_request(context, "/api/papers").json() == []
                        completed.append("real_http_matrix_preview_is_read_only")
                        page.locator('[data-action="matrix-confirm"]').click()
                        page.locator("#matrix-import-preview").filter(
                            has_text="已保存：新增 2 篇论文").wait_for(state="visible")
                        assert len(session_request(context, "/api/papers").json()) == 2
                        completed.append("real_http_matrix_confirmation_persists_selected_records")

                        with page.expect_download() as pending:
                            page.locator('a[href*="/matrix?format=csv"]').click()
                        matrix_download = pending.value
                        matrix_path = output.parent / (output.stem + "-matrix.csv")
                        matrix_download.save_as(str(matrix_path))
                        matrix_csv = matrix_path.read_text(encoding="utf-8")
                        assert "paper_evidence_id" in matrix_csv and "association_evidence_ids" in matrix_csv
                        completed.append("real_http_matrix_csv_download")

                        with page.expect_download() as pending:
                            page.locator('.run-controls a[href*="/export"]').first.click()
                        run_download = pending.value
                        run_path = output.parent / (output.stem + "-run-export.zip")
                        run_download.save_as(str(run_path))
                        assert run_path.stat().st_size > 0
                        completed.append("real_http_run_export_download")

                        metrics = client.get("/__smoke__/metrics").json()
                        assert metrics["agent_model_calls"] == 10, metrics
                        assert {"api.openai.com", "export.arxiv.org", "api.github.com"}.issubset(
                            metrics["by_host"]), metrics
                        assert not page_errors, page_errors
                        assert not non_local_requests, non_local_requests
                        assert len(browser_requests) > 30, len(browser_requests)
                        browser.close()

                        report = {
                            "mode": ("Chromium over a separate real loopback HTTPS process with an ephemeral test cert"
                                     if hosted else
                                     "Chromium over a separate real loopback HTTP process"),
                            "browser_network_tested": True,
                            "external_network_tested": False,
                            "identity": ("two isolated hosted guest cookies; private re0.test name resolves only to loopback"
                                         if hosted else
                                         "local single-owner mode; hosted isolation not exercised"),
                            "fixture_agent_model_calls": metrics["agent_model_calls"],
                            "fixture_provider_requests": metrics["mock_requests"],
                            "fixture_provider_requests_by_host": metrics["by_host"],
                            "local_browser_request_count": len(browser_requests),
                            "steps": completed,
                            "page_errors": page_errors,
                            "non_local_requests": non_local_requests,
                        }
            finally:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=3)

    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return report


if __name__ == "__main__":
    target = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / ".data/agent-real-http-browser-smoke.json"
    run(target)
