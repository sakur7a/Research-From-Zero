"""Real Chromium + real loopback HTTP for the Skill-result-to-review flow.

Unlike the TestClient bridge smokes, this starts `run.py` as a separate process and lets Chromium
make ordinary TCP requests to it. It uses only the checked-in fictional result fixture; model and
research-provider calls are never made. This checks same-origin browser/API wiring, not TLS,
reverse-proxy behaviour, a second account or public deployment.

Requires optional `playwright` and a Chromium executable (CHROMIUM_PATH).
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time

import httpx
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError, sync_playwright

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests" / "fixtures" / "search-result.json"


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def run(output: Path) -> dict:
    output.parent.mkdir(parents=True, exist_ok=True)
    errors: list[str] = []
    browser_requests: list[str] = []
    completed: list[str] = []
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp:
        directory = Path(temp)
        port = free_port()
        database = directory / "real-http.sqlite3"
        env = {key: value for key, value in os.environ.items()
               if not key.startswith("RE0_") and key not in {"TAVILY_API_KEY", "GITHUB_TOKEN"}}
        env.update(RE0_DB=str(database), RE0_HOST="127.0.0.1", RE0_PORT=str(port))
        log_path = directory / "server.log"
        with log_path.open("w", encoding="utf-8") as log:
            process = subprocess.Popen([sys.executable, "run.py"], cwd=ROOT, env=env,
                                       stdout=log, stderr=subprocess.STDOUT)
            base = f"http://127.0.0.1:{port}"
            try:
                with httpx.Client(base_url=base, timeout=2, trust_env=False) as client:
                    deadline = time.monotonic() + 20
                    while True:
                        try:
                            ready = client.get("/api/health")
                            if ready.status_code == 200:
                                break
                        except httpx.HTTPError:
                            pass
                        if process.poll() is not None or time.monotonic() >= deadline:
                            raise RuntimeError("本机服务未能启动：" + log_path.read_text(encoding="utf-8")[-1000:])
                        time.sleep(0.1)

                    with sync_playwright() as engine:
                        launch = {"headless": True}
                        if os.getenv("CHROMIUM_PATH"):
                            launch["executable_path"] = os.environ["CHROMIUM_PATH"]
                        browser = engine.chromium.launch(**launch)
                        context = browser.new_context(viewport={"width": 1440, "height": 1000})

                        def guard_request(route):
                            url = route.request.url
                            if url.startswith(base + "/"):
                                browser_requests.append(url)
                                route.continue_()
                            else:
                                errors.append("browser attempted non-local request: " + url)
                                route.abort()

                        context.route("**/*", guard_request)
                        page = context.new_page()
                        page.on("pageerror", lambda error: errors.append(str(error)))

                        # Import the Skill result through the actual file picker and same-origin API.
                        page.goto(base + "/static/search.html", wait_until="networkidle")
                        page.locator("#file").set_input_files(str(FIXTURE))
                        page.locator(".fact-grid").wait_for()
                        assert "测试夹具" in page.locator("#workbench").inner_text()
                        completed.append("real_http_load_of_versioned_skill_result")

                        page.get_by_role("button", name="导出与入库").click()
                        page.get_by_role("button", name="预览导入").click()
                        page.locator(".import-report").wait_for()
                        assert "可导入 2 篇" in page.locator(".import-report").inner_text()
                        assert client.get("/api/papers").json() == [], "preview must not write"
                        completed.append("real_http_import_preview_writes_nothing")

                        page.get_by_role("button", name="写入文献库").click()
                        page.locator(".import-report").filter(has_text="已写入").wait_for()
                        papers = client.get("/api/papers").json()
                        assert len(papers) == 2 and sum(len(item["resources"]) for item in papers) == 2
                        completed.append("real_http_import_commit_persists_source_audits")

                        # The library reads that same persisted row and appends a user review to it.
                        page.goto(base + "/library", wait_until="networkidle")
                        page.locator(".paper-title").filter(has_text="LayerKit:").click()
                        page.locator("dialog.drawer").wait_for()
                        page.locator(".resource-panel [data-evidence]").first.click()
                        page.locator("#evidence-content").wait_for()
                        page.locator("#revise-audit").click()
                        review = page.locator("#audit-confirmation-form")
                        review.locator("[name=attribution]").select_option("third_party")
                        review.locator("[name=version_match]").select_option("unknown")
                        review.locator("[name=version_source_url]").fill("https://example.org/version-review")
                        review.locator("[name=attribution_source_url]").fill(
                            "https://example.org/fixture-evidence")
                        review.locator("[name=attribution_excerpt]").fill(
                            "虚构夹具：本次只验证 HTTP 写入流程，不代表真实作者归属。")
                        review.locator("[name=review_note]").fill(
                            "fixture-only review; no real authorship conclusion")
                        review.locator("[type=submit]").click()
                        try:
                            review.wait_for(state="detached", timeout=10000)
                        except PlaywrightTimeoutError as exc:
                            message = page.locator("#audit-confirmation-form .form-error").inner_text()
                            raise AssertionError("复核提交失败：" + message) from exc
                        page.locator(".record-provenance").wait_for()
                        assert "人工复核" in page.locator(".record-provenance").inner_text()
                        reviewed = next(item for item in client.get("/api/papers").json()
                                        if item["title"].startswith("LayerKit:"))
                        records = client.get(
                            f"/api/resources/{reviewed['resources'][0]['id']}/observations").json()
                        assert [row["record_kind"] for row in records] == ["confirmation", "observation"]
                        assert records[0]["attribution"] == "third_party"
                        assert records[1]["attribution"] == "official"
                        completed.append("real_http_review_is_append_only_and_survives_readback")

                        page.set_viewport_size({"width": 390, "height": 844})
                        dialog_box = page.locator("dialog.modal-wide").bounding_box()
                        assert dialog_box and dialog_box["x"] >= 0 \
                            and dialog_box["x"] + dialog_box["width"] <= 390
                        completed.append("real_http_resource_history_fits_mobile_viewport")
                        assert not errors, errors
                        browser.close()

                    report = {
                        "mode": "Chromium over a separate real loopback HTTP process",
                        "browser_network_tested": True,
                        "external_network_tested": False,
                        "model_calls": 0,
                        "completed": completed,
                        "local_request_count": len(browser_requests),
                        "non_local_requests_or_page_errors": errors,
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
    target = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / ".data/real-http-browser-smoke.json"
    run(target)
