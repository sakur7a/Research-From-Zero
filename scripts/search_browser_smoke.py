"""Offline Chromium smoke for the retrieval workbench (`web/search.html`).

Same harness as `browser_smoke.py`: a real FastAPI TestClient in-process, `window.fetch` bridged to
it, and the page's modules concatenated into one script. Nothing reaches the network — the result
file the page reads is the checked-in fixture, which is fictional and says so.

What this proves is the wiring: the file input parses a real `--json` result, the four views render
from it, the matrix rows match the pure logic the Node tests pin, the clipboard gets BibTeX, and the
library import posts to an endpoint that already existed. It does not test browser TCP, CORS or a
deployment.

Requires optional `playwright` and a Chromium executable (CHROMIUM_PATH).
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

import httpx
from fastapi.testclient import TestClient
from playwright.sync_api import sync_playwright
from re0.main import create_app

FIXTURE = ROOT / "tests" / "fixtures" / "search-result.json"
MODULES = ["icons.js", "core.js", "api.js", "theme.js", "search-core.js", "search.js"]


HEADERS = {"X-Re0-Client": "web", "Content-Type": "application/json"}


def library(client) -> list:
    return client.get("/api/papers", headers=HEADERS).json()


def bundle(name: str) -> str:
    """One module's source, with its import/export lines removed so they can share a scope.

    `[^;]*?` rather than `.*?`: an import may span several lines, and a dot does not match a newline,
    so the single-line pattern silently leaves a multi-line import in place — and one unresolvable
    import is enough to stop the whole script from running.
    """
    text = (ROOT / "web" / name).read_text(encoding="utf-8")
    text = re.sub(r"^import\s+[^;]*?;[ \t]*$", "", text, flags=re.M)
    text = re.sub(r"\bexport (?=(?:async )?(?:function|const|let|class))", "", text)
    # `export:` as an object key is fine; module syntax is not. Only the forms that would fail to
    # resolve in a bundled scope are looked for here.
    leftover = re.findall(r"^\s*(?:import\s[^;]*;|export\s+(?:\{|\*|default)).*$", text, flags=re.M)
    assert not leftover, f"{name} still has module syntax the bundle cannot run: {leftover}"
    return text


def run(output_dir: Path):
    output_dir.mkdir(parents=True, exist_ok=True)
    errors, completed, requests = [], [], []

    def no_network(request):
        raise AssertionError(f"Offline browser test must not contact external providers: {request.url}")

    css = "\n".join((ROOT / "web" / name).read_text(encoding="utf-8")
                    for name in ("theme.css", "agent.css", "search.css"))
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp, \
            TestClient(create_app(str(Path(temp) / "workbench.sqlite3"),
                                  httpx.MockTransport(no_network))) as client, \
            sync_playwright() as engine:
        launch = {"headless": True}
        if os.getenv("CHROMIUM_PATH"):
            launch["executable_path"] = os.getenv("CHROMIUM_PATH")
        browser = engine.chromium.launch(**launch)
        context = browser.new_context(viewport={"width": 1440, "height": 1050},
                                      permissions=["clipboard-read", "clipboard-write"])
        page = context.new_page()
        page.on("pageerror", lambda error: errors.append(str(error)))
        console = []
        page.on("console", lambda message: console.append(f"{message.type}: {message.text}")
                if message.type in ("error", "warning") else None)

        def request_bridge(path, options):
            assert path.startswith("/api/"), path
            requests.append((options.get("method", "GET"), path))
            response = client.request(options.get("method", "GET"), path,
                                      headers=options.get("headers"), content=options.get("body"))
            return {"status": response.status_code, "body": response.text}

        page.expose_function("re0TestRequest", request_bridge)
        page.set_content('<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">'
                         '<meta name="viewport" content="width=device-width, initial-scale=1">'
                         f'<style>{css}</style></head><body><aside class="side"></aside>'
                         '<div class="shell"><header class="topbar"><span id="crumb"></span></header>'
                         '<main id="workbench" class="workbench" tabindex="-1"></main></div>'
                         '<div id="notice" role="status" aria-live="polite"></div></body></html>')
        page.evaluate('''() => { window.fetch = async (path, options = {}) => {
          const result = await window.re0TestRequest(path, {method:options.method||'GET',
            headers:options.headers||{}, body:options.body});
          return new Response(result.status===204?null:result.body, {status:result.status,
            headers:{'Content-Type':'application/json'}});
        }; }''')
        page.add_script_tag(content="\n".join(bundle(name) for name in MODULES)
                            + "\nwindow.re0Workbench = {state, candidates, bibtex, matrix, summary};",
                            type="module")

        # 1. The empty state says what to run, and does not pretend to search.
        try:
            page.wait_for_selector("#file", timeout=15000)
        except Exception as exc:
            # A page that never rendered is a module failure; the timeout alone does not say which.
            raise AssertionError(f"页面没有渲染：{exc}; page errors={errors}; console={console}") from exc
        assert "re0 paper search --json" in page.inner_text("#workbench")
        assert not page.query_selector("#candidate-list"), "nothing is rendered before a file is read"
        page.screenshot(path=str(output_dir / "empty.png"), full_page=True)
        completed.append("empty_state_names_the_command_that_produces_the_file")

        # 2. A file that is not a result is refused with a reason, and the page stays usable.
        bad = Path(temp) / "not-a-result.json"
        bad.write_text('{"schema_version":"1","rows":[],"approval":[]}', encoding="utf-8")
        page.set_input_files("#file", str(bad))
        page.wait_for_selector(".notice.error")
        assert "--resource-matrix" in page.inner_text(".notice.error")
        completed.append("a_matrix_file_is_named_instead_of_rendered_as_an_empty_search")

        # 3. The real fixture loads into the coverage view.
        page.set_input_files("#file", str(FIXTURE))
        page.wait_for_selector(".fact-grid")
        text = page.inner_text("#workbench")
        for expected in ("layer decomposition", "semanticscholar", "429", "虚构", "2023", "partial"):
            assert expected in text, expected
        # A failed source is shown as a failure next to its own count, never as a zero result.
        assert page.locator(".source-list .chip.bad").count() == 1
        page.screenshot(path=str(output_dir / "coverage.png"), full_page=True)
        completed.append("coverage_view_shows_the_window_the_sources_and_the_failures")

        # 4. Candidates: four papers, filters narrow the view, and the note travels with the record.
        page.get_by_role("button", name="候选论文").click()
        page.wait_for_selector("#candidate-list")
        assert page.locator(".candidate").count() == 4
        assert "共 4 篇" in page.inner_text("#candidate-count")
        page.fill("#f-query", "LayerKit")
        page.wait_for_function("document.querySelectorAll('.candidate').length === 1")
        assert "1 篇被筛选条件挡住了" not in page.inner_text("#candidate-count")
        assert "3 篇被筛选条件挡住了" in page.inner_text("#candidate-count")
        page.fill("#f-query", "")
        page.wait_for_function("document.querySelectorAll('.candidate').length === 4")
        page.select_option("#f-publication", "preprint")
        page.wait_for_function("document.querySelectorAll('.candidate').length === 1")
        assert "仅见预印本版本" in page.inner_text(".candidate")
        page.select_option("#f-publication", "")
        page.wait_for_function("document.querySelectorAll('.candidate').length === 4")
        # The label comes from the payload, so the raw token is shown beside it.
        assert "（preprint）" in page.inner_text("#candidate-list")
        assert "（venue）" in page.inner_text("#candidate-list")
        page.screenshot(path=str(output_dir / "candidates.png"), full_page=True)
        completed.append("candidates_render_and_filters_only_narrow_the_view")

        # 5. The matrix keeps a row for a paper with no candidate, and says which absence it is.
        page.get_by_role("button", name="资源审计矩阵").click()
        page.wait_for_selector("table.matrix")
        assert page.locator("table.matrix tbody tr").count() == 4
        body = page.inner_text("table.matrix")
        assert "检查范围内未找到" in body and "检索未完成" in body
        assert "归属待确认" in body and "疑似 adapter" in body
        page.locator("table.matrix .row-toggle").first.click()
        page.wait_for_selector("table.matrix tr.detail")
        detail = page.inner_text("table.matrix tr.detail")
        # The licences and the links a conclusion rests on travel with the row, not with a verdict.
        assert "MIT" in detail and "cc-by-nc-4.0" in detail and "检查于" in detail, detail
        assert "github.com/example-lab/layerkit" in page.inner_html("table.matrix tr.detail")
        assert "javascript:" not in page.inner_html("table.matrix")
        page.screenshot(path=str(output_dir / "matrix.png"), full_page=True)
        completed.append("matrix_rows_carry_their_blockers_and_their_sources")

        # 6. BibTeX reaches the clipboard, escaped, from the papers actually shown.
        page.get_by_role("button", name="导出与入库").click()
        page.wait_for_selector(".export-grid")
        page.get_by_role("button", name=re.compile("复制当前显示的")).click()
        page.wait_for_function("document.querySelector('#notice').classList.contains('show')")
        assert "已复制到剪贴板" in page.inner_text("#notice"), page.inner_text("#notice")
        citation = page.evaluate(
            "re0Workbench.bibtex(re0Workbench.candidates(re0Workbench.state.result))")
        assert citation.count("@misc{re0_") == 4, citation[:200]
        assert "author = {Wei Chen and Amara Osei and Liu Yang}" in citation
        assert "preprint only in the sources this search reached" in citation
        completed.append("bibtex_for_the_visible_candidates_reaches_the_clipboard")

        # 7. The import previews against an endpoint that already existed, then writes on a second,
        #    separate click. Two papers in the fixture have an audited candidate; two do not.
        assert "2 篇带有可导入的审计记录" in page.inner_text("#import-card")
        page.get_by_role("button", name="预览导入").click()
        page.wait_for_selector(".import-report")
        report = page.inner_text(".import-report")
        assert report.startswith("预览") and "可导入 2 篇" in report, report
        assert library(client) == [], "a preview must not write"
        page.get_by_role("button", name="写入文献库").click()
        page.wait_for_function(
            "document.querySelector('.import-report') && "
            "!document.querySelector('.import-report').textContent.startsWith('预览')")
        papers = library(client)
        assert sorted(item["title"] for item in papers) == [
            "LayerKit: Layered Scene Generation with Decomposed Diffusion",
            "LayoutDiff: Controllable Layout Generation"], papers
        assert len(papers[0]["resources"]) + len(papers[1]["resources"]) == 2
        # Importing the same file again links nothing twice. The commit button is disabled until a
        # fresh preview exists, so a second write is something the reader has to look at again.
        assert page.get_by_role("button", name="写入文献库").is_disabled()
        page.get_by_role("button", name="预览导入").click()
        page.wait_for_function(
            "document.querySelector('.import-report').textContent.startsWith('预览')")
        page.get_by_role("button", name="写入文献库").click()
        page.wait_for_function(
            "document.querySelector('.import-report').textContent.includes('已写入')")
        assert "同一次检查已导入过" in page.inner_text(".import-report")
        assert len(library(client)) == 2
        page.screenshot(path=str(output_dir / "export.png"), full_page=True)
        completed.append("library_import_previews_then_writes_through_an_existing_endpoint")

        # 8. A narrow viewport still fits, because the matrix scrolls instead of widening the page.
        page.set_viewport_size({"width": 390, "height": 844})
        page.get_by_role("button", name="资源审计矩阵").click()
        page.wait_for_selector("table.matrix")
        page.screenshot(path=str(output_dir / "mobile.png"), full_page=True)
        assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
        completed.append("mobile_layout_scrolls_the_matrix_instead_of_overflowing")

        assert not errors, errors
        assert [path for _, path in requests] == ["/api/import/resource-audits"] * 4, requests
        # 先落盘再关闭浏览器：个别环境在浏览器进程回收阶段会中断，结果不应丢失。
        report_body = {"mode": "offline Chromium DOM + real FastAPI TestClient bridge",
                       "page": "/static/search.html",
                       "fixture": str(FIXTURE.relative_to(ROOT)),
                       "browser_network_tested": False, "external_network_tested": False,
                       "api_calls": requests, "completed": completed, "page_errors": errors}
        (output_dir / "search-browser-report.json").write_text(
            json.dumps(report_body, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(report_body, ensure_ascii=False, indent=2))
        browser.close()


if __name__ == "__main__":
    output = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "test-results" / "search-browser"
    run(output)
