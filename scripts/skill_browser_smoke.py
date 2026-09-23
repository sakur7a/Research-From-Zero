"""Offline Chromium check for the self-contained, static Skill demo page.

The page is served by Python's static file handler on loopback. No Re0 API or model
service is started, and browser requests outside that one origin are blocked.
"""
from __future__ import annotations

from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import sys
from threading import Thread
from urllib.parse import urlsplit

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]


class QuietStaticHandler(SimpleHTTPRequestHandler):
    def log_message(self, _format: str, *_args: object) -> None:
        pass


def run(output_dir: Path) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    server = ThreadingHTTPServer(
        ("127.0.0.1", 0),
        partial(QuietStaticHandler, directory=str(ROOT / "web")),
    )
    worker = Thread(target=server.serve_forever, daemon=True)
    worker.start()
    host, port = server.server_address[:2]
    origin = f"http://{host}:{port}"
    blocked: list[str] = []
    requests: list[str] = []
    responses: list[tuple[int, str]] = []
    page_errors: list[str] = []
    completed: list[str] = []

    try:
        with sync_playwright() as engine:
            launch = {"headless": True}
            executable = os.getenv("CHROMIUM_PATH")
            if executable:
                launch["executable_path"] = executable
            browser = engine.chromium.launch(**launch)
            context = browser.new_context(viewport={"width": 1440, "height": 1050})
            context.grant_permissions(["clipboard-read", "clipboard-write"], origin=origin)

            def same_origin_only(route) -> None:
                target = route.request.url
                if urlsplit(target).netloc != f"{host}:{port}":
                    blocked.append(target)
                    route.abort()
                    return
                requests.append(target)
                route.continue_()

            context.route("**/*", same_origin_only)
            page = context.new_page()
            page.on("pageerror", lambda error: page_errors.append(str(error)))
            page.on("response", lambda response: responses.append((response.status, response.url)))

            response = page.goto(f"{origin}/skill.html", wait_until="networkidle")
            assert response and response.status == 200
            page.locator(".skill-intro h1").wait_for()
            assert "找到候选之后" in page.locator(".skill-intro h1").inner_text()
            assert "这是静态演示页" in page.locator(".static-notice").inner_text()
            assert "本页不发起实时请求" in page.locator(".static-notice").inner_text()
            assert "2106.09685" in page.locator(".paper-trail").inner_text()
            assert "作者归属尚未确认" in page.locator(".paper-trail").inner_text()
            completed.append("static_historical_provenance_and_scope_are_visible")
            page.screenshot(path=str(output_dir / "skill-desktop.png"), full_page=True)

            observation_tab = page.get_by_role("tab", name="资源观察")
            limits_tab = page.get_by_role("tab", name="未确认项")
            assert observation_tab.get_attribute("aria-selected") == "true"
            page.locator("#panel-matrix").wait_for(state="visible")
            limits_tab.focus()
            page.keyboard.press("ArrowLeft")
            assert observation_tab.get_attribute("aria-selected") == "true"
            page.keyboard.press("ArrowRight")
            assert limits_tab.get_attribute("aria-selected") == "true"
            assert "attribution=unconfirmed" in page.locator("#panel-limits").inner_text()
            completed.append("audit_tabs_support_click_and_keyboard_navigation")
            page.screenshot(path=str(output_dir / "skill-limits.png"), full_page=True)

            page.locator("#copy-command").click()
            page.wait_for_function("document.querySelector('#skill-notice')?.classList.contains('show')")
            command = page.locator("#skill-command").inner_text()
            copied = page.evaluate("navigator.clipboard.readText()")
            assert copied == command
            assert "页面没有执行它" in page.locator("#skill-notice").inner_text()
            completed.append("copy_action_only_copies_the_local_skill_command")

            observation_tab.click()
            page.wait_for_function("!document.querySelector('#skill-notice')?.classList.contains('show')")
            page.wait_for_timeout(250)
            page.set_viewport_size({"width": 390, "height": 844})
            page.evaluate("window.scrollTo(0, 0)")
            assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
            page.screenshot(path=str(output_dir / "skill-mobile.png"), full_page=True)
            completed.append("desktop_and_mobile_layout_have_no_horizontal_overflow")

            assert not blocked, blocked
            assert all(status == 200 for status, _ in responses), responses
            assert not page_errors, page_errors
            assert all(urlsplit(url).netloc == f"{host}:{port}" for url in requests), requests
            assert not any("/api/" in urlsplit(url).path for url in requests), requests
            completed.append("all_assets_are_local_and_the_demo_makes_no_api_requests")

            report = {
                "mode": "offline Chromium + static loopback file server",
                "backend_started": False,
                "external_requests": blocked,
                "requests": [urlsplit(url).path for url in requests],
                "responses": responses,
                "completed": completed,
                "page_errors": page_errors,
            }
            (output_dir / "skill-browser-report.json").write_text(
                json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
            print(json.dumps(report, ensure_ascii=False, indent=2))
            browser.close()
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=3)


if __name__ == "__main__":
    output = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "test-results" / "skill-browser"
    run(output)
