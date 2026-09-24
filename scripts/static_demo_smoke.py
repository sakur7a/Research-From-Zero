"""Browser acceptance for the built static demo or its public Vercel URL."""

import argparse
import json
import os
from pathlib import Path

from playwright.sync_api import sync_playwright


def run(origin: str) -> None:
    errors = []
    forbidden = []
    bad_responses = []
    with sync_playwright() as playwright:
        launch = {"headless": True}
        if os.getenv("CHROMIUM_PATH"):
            launch["executable_path"] = os.environ["CHROMIUM_PATH"]
        browser = playwright.chromium.launch(**launch)
        context = browser.new_context(viewport={"width": 1440, "height": 900}, accept_downloads=True)
        context.grant_permissions(["clipboard-read", "clipboard-write"], origin=origin)
        page = context.new_page()
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.on("request", lambda request: forbidden.append(request.url) if
                (request.url.startswith(("http://127.0.0.1", "http://localhost")) and
                 not origin.startswith(("http://127.0.0.1", "http://localhost"))) or
                "/api/" in request.url or "openai.com" in request.url else None)
        page.on("response", lambda response: bad_responses.append((response.status, response.url))
                if response.status >= 400 else None)
        assert page.goto(origin + "/", wait_until="networkidle").status == 200
        assert "从论文候选" in page.locator("h1").inner_text()
        page.locator('[data-theme-toggle]').click()
        assert page.locator("html").get_attribute("data-theme") == "dark"
        page.reload(wait_until="networkidle")
        assert page.locator("html").get_attribute("data-theme") == "dark"
        page.locator('[data-theme-toggle]').click()
        page.get_by_role("button", name="打开历史案例").click()
        page.get_by_role("button", name="候选论文").click()
        assert "LoRA: Low-Rank" in page.locator("#candidate-list").inner_text()
        page.locator("#f-query").fill("no-such-paper")
        assert "没有符合条件" in page.locator("#candidate-list").inner_text()
        page.locator("#f-query").fill("LoRA")
        page.get_by_role("button", name="资源审计矩阵").click()
        assert "partially_available" in page.locator(".matrix-wrap").inner_text()
        page.locator("[data-action=row]").first.click()
        assert "2026-09-22" in page.locator("tr.detail").inner_text()
        assert "github.com/microsoft/LoRA" in page.locator("tr.detail").inner_text()
        page.locator('[data-view="export"]').click()
        page.locator('[data-action="copy-bibtex-shown"]').click()
        assert "LoRA" in page.evaluate("navigator.clipboard.readText()")
        with page.expect_download() as download:
            page.locator('[data-action="download-csv"]').click()
        assert download.value.suggested_filename.endswith(".csv")
        page.evaluate("""() => {
          navigator.clipboard.writeText = () => Promise.reject(new Error('denied'));
          document.execCommand = () => false;
          URL.createObjectURL = () => { throw new Error('blocked'); };
        }""")
        page.locator('[data-action="copy-csv"]').click()
        assert "复制失败" in page.locator("#notice").inner_text()
        page.locator('[data-action="download-json"]').click()
        assert "下载失败" in page.locator("#notice").inner_text()
        page.locator('[data-action="back-to-load"]').click()
        page.locator("details.own-result summary").click()
        page.locator("#paste").fill("{bad")
        page.locator('[data-action="parse-paste"]').click()
        assert "不是合法 JSON" in page.locator(".demo-start .notice.error").inner_text()
        hostile = json.loads((Path(__file__).resolve().parents[1] / "samples" /
                              "lora-2026-09-22.json").read_text(encoding="utf-8"))
        hostile["documents"][0]["paper"]["title"] = '<img src=x onerror="window.xss=1">'
        hostile["documents"][0]["resource_audits"][0]["resource_url"] = "javascript:alert(1)"
        page.locator("details.own-result summary").click()
        page.locator("#paste").fill(json.dumps(hostile))
        page.locator('[data-action="parse-paste"]').click()
        page.locator('[data-view="candidates"]').click()
        assert page.locator("#candidate-list img").count() == 0
        assert page.evaluate("window.xss === undefined")
        assert page.locator('#candidate-list a[href^="javascript:"]').count() == 0
        page.locator('[data-action="back-to-load"]').click()
        page.locator("details.own-result summary").click()
        page.locator("#file").set_input_files(str(Path(__file__).resolve().parents[1] /
                                              "samples" / "lora-2026-09-22.json"))
        assert "历史案例" not in page.locator("#crumb").inner_text()
        assert page.goto(origin + "/skill.html", wait_until="networkidle").status == 200
        page.get_by_role("tab", name="未确认项").focus()
        page.keyboard.press("ArrowLeft")
        page.keyboard.press("ArrowRight")
        assert "attribution=unconfirmed" in page.locator("#panel-limits").inner_text()
        page.set_viewport_size({"width": 390, "height": 844})
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        assert page.goto(origin + "/search.html", wait_until="networkidle").status == 200
        assert "从论文候选" in page.locator("h1").inner_text()
        page.locator('[data-action="menu"]').click()
        assert "mobile-expanded" in page.locator(".side").get_attribute("class")
        assert context.request.get(origin + "/static/search.html").status == 200
        assert context.request.get(origin + "/static/skill.html").status == 200
        assert not errors, errors
        assert not forbidden, forbidden
        assert not [item for item in bad_responses if item[1].endswith((".js", ".css", ".svg", ".json"))], bad_responses
        assert context.request.get(origin + "/api/health").status == 404
        assert context.request.get(origin + "/missing-page").status == 404
        print(f"PASS static browser flow: {origin}; no page errors or forbidden requests")
        browser.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("origin")
    args = parser.parse_args()
    run(args.origin.rstrip("/"))
