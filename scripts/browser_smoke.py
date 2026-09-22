"""Offline DOM/API integration test using Chromium + real FastAPI TestClient.

No browser network access is needed: window.fetch is bridged to TestClient.
This deliberately does not claim to test browser TCP, CORS, or HTTPS deployment.
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
sys.path.insert(0, str(ROOT / "backend" / "tests"))

import httpx
from fastapi.testclient import TestClient
from playwright.sync_api import sync_playwright
from re0.main import create_app
from test_zotero import KEY as ZOTERO_KEY, ZoteroService, item as zotero_item


def run(output_dir: Path):
    output_dir.mkdir(parents=True, exist_ok=True)
    errors = []
    completed = []
    requests = []
    # One fixture library, served in-process. Anything that is not api.zotero.org still fails the
    # run, so "offline" keeps meaning offline: this replaces no provider, it stands in for one.
    zotero_service = ZoteroService(
        items=[zotero_item("AAAA0001", 300, "UI 同步进来的论文", doi="10.9999/ui-zotero",
                           collections=["COLL0001"], abstract="fixture abstract")],
        library_version=300)
    def no_network(request):
        if request.url.host == "api.zotero.org":
            return zotero_service(request)
        raise AssertionError("Offline browser test must not contact external providers")
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp, TestClient(create_app(str(Path(temp)/"browser.sqlite3"), httpx.MockTransport(no_network))) as client, sync_playwright() as engine:
        launch = {"headless": True}
        executable = os.getenv("CHROMIUM_PATH")
        if executable:
            launch["executable_path"] = executable
        browser = engine.chromium.launch(**launch)
        page = browser.new_page(viewport={"width":1440,"height":1050}, device_scale_factor=1)
        page.on("pageerror", lambda error: errors.append(str(error)))
        def request_bridge(path, options):
            assert path.startswith("/api/"), path
            requests.append((options.get("method", "GET"), path))
            response = client.request(options.get("method", "GET"), path, headers=options.get("headers"), content=options.get("body"))
            return {"status": response.status_code, "body": response.text}
        page.expose_function("re0TestRequest", request_bridge)
        css = (ROOT/"web/theme.css").read_text(encoding="utf-8")+"\n"+(ROOT/"web/styles.css").read_text(encoding="utf-8")
        page.set_content('<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><style>'+css+'</style></head><body><div id="app"></div><div id="overlays"></div><div id="toasts" role="status" aria-live="polite"></div></body></html>')
        page.evaluate('''() => { window.fetch = async (path, options = {}) => {
          const result = await window.re0TestRequest(path, {method:options.method||'GET',headers:options.headers||{},body:options.body});
          return new Response(result.status===204?null:result.body, {status:result.status,headers:{'Content-Type':'application/json'}});
        }; }''')
        js = []
        for name in ["icons.js", "core.js", "api.js", "theme.js", "app.js"]:
            text = (ROOT/"web"/name).read_text(encoding="utf-8")
            text = re.sub(r"^import .*?;\n", "", text, flags=re.M)
            text = re.sub(r"\bexport (?=(?:async )?(?:function|const|let|class))", "", text)
            js.append(text)
        page.add_script_tag(content="\n".join(js)+"\nwindow.re0Reload=refresh;", type="module")
        page.get_by_text("你的下一项研究，从这里开始。", exact=True).wait_for()
        page.screenshot(path=str(output_dir/"empty.png"),full_page=True)
        completed.append("initial_empty_state")
        page.get_by_role("button", name="体验演示数据").click()
        page.locator('.paper-card').first.wait_for()
        assert page.locator('.paper-card').count()==6
        assert requests.count(('POST','/api/demo'))==1
        page.locator('#toasts').evaluate('(node)=>node.replaceChildren()')
        page.screenshot(path=str(output_dir/"library.png"),full_page=True)
        completed.append("load_fictional_demo")
        page.locator('#search').fill('Layered Canvas')
        assert page.locator('.paper-card').count()==1
        page.locator('#search').fill('')
        assert page.locator('.paper-card').count()==6
        page.locator('[data-select]').nth(0).check()
        page.locator('[data-select]').nth(1).check()
        page.locator('[data-action="compare-selected"]').click()
        assert page.locator('.compare-table').count()==1
        page.screenshot(path=str(output_dir/"compare.png"),full_page=True)
        completed.append("search_select_compare")
        page.locator('nav [data-nav="graph"]').click()
        assert page.locator('.graph-paper').count()==6
        page.screenshot(path=str(output_dir/"graph.png"),full_page=True)
        page.locator('nav [data-nav="library"]').click()
        completed.append("graph_from_saved_relations")
        page.locator('.paper-title').filter(has_text='Layered Canvas').click()
        page.locator('dialog.drawer').wait_for()
        page.locator('[data-evidence]').first.click()
        page.locator('#evidence-content').wait_for()
        page.screenshot(path=str(output_dir/"evidence.png"),full_page=True)
        assert page.locator('#evidence-content').inner_text().find('演示')>=0
        page.keyboard.press('Escape')
        page.keyboard.press('Escape')
        completed.append("open_evidence_history")
        page.locator('.page-actions [data-action="add-paper"]').click()
        form=page.locator('#paper-form')
        title='UI test <img src=x onerror=alert(1)> & evidence'
        form.locator('[name=title]').fill(title)
        form.locator('[name=authors]').fill('Test Author; Second Author')
        form.locator('[name=topics]').first.check()
        form.locator('[name=notes]').fill('First note: must survive editing.')
        form.locator('[type=submit]').click()
        page.get_by_role('heading',name=title,exact=True).wait_for()
        assert page.locator('.drawer-title img').count()==0
        completed.append("create_paper_and_escape_html")
        page.locator('#edit-paper').click()
        form=page.locator('#paper-form')
        form.locator('[name=status]').select_option('baseline')
        form.locator('[name=notes]').fill('Edited note: durable research context.')
        form.locator('[type=submit]').click()
        page.locator('dialog.drawer .badge-baseline').wait_for()
        page.locator('#add-resource').click()
        form=page.locator('dialog.modal form')
        form.locator('[name=label]').fill('Example unsupported link')
        form.locator('[name=url]').fill('https://example.org/resource')
        form.locator('[type=submit]').click()
        page.locator('[data-check]').wait_for()
        page.locator('[data-check]').click()
        page.locator('.resource-panel .badge-unsupported').wait_for()
        page.locator('[data-evidence]').click()
        page.locator('#evidence-content').wait_for()
        assert '未完成验证' in page.locator('#evidence-content').inner_text()
        page.keyboard.press('Escape')
        page.keyboard.press('Escape')
        completed.append("edit_notes_and_record_unsupported_check")
        # Import via the actual file-picker workflow and real backend preview.
        page.locator('.page-actions [data-action="import"]').click()
        content=json.dumps([{"title":"Imported CSL paper","DOI":"10.1234/ui-csl","author":[{"given":"CSL","family":"Author"}]}])
        page.locator('#import-file').set_input_files({"name":"library.json","mimeType":"application/json","buffer":content.encode()})
        page.locator('#confirm-import').wait_for()
        page.locator('#confirm-import').click()
        page.locator('.paper-title').filter(has_text='Imported CSL paper').wait_for()
        completed.append("csl_file_preview_and_import")
        # Zotero read-only sync through the same dialog workflow: credentials, collection scoping,
        # preview, then an explicit commit. The preview must write nothing.
        page.locator('[data-action="settings"]').first.click()
        assert '只读增量同步' in page.locator('dialog.modal').inner_text()
        assert '远端 API 同步尚未实现' not in page.locator('dialog.modal').inner_text()
        page.locator('#settings-zotero').click()
        page.locator('#zotero-form').wait_for()
        page.locator('#zotero-form [name=library_id]').fill('12345')
        page.locator('#zotero-form [name=api_key]').fill(ZOTERO_KEY)
        page.locator('#zotero-form [name=label]').fill('UI 测试库')
        page.locator('#zotero-collections').click()
        page.locator('#zotero-scope input[name=collection]').wait_for()
        assert page.locator('#zotero-scope').inner_text().find('图层分解') >= 0
        page.locator('#zotero-scope input[name=collection]').first.check()
        page.screenshot(path=str(output_dir/'library-zotero-scope.png'), full_page=True)
        page.locator('#zotero-preview').click()
        page.locator('#zotero-apply').wait_for()
        preview_text = page.locator('#zotero-result').inner_text()
        assert '这是预览' in preview_text and '游标 0 → 300' in preview_text, preview_text
        assert 'UI 同步进来的论文' in preview_text
        papers_before = len(client.get('/api/papers').json())
        page.screenshot(path=str(output_dir/'library-zotero-preview.png'), full_page=True)
        page.locator('#zotero-apply').click()
        page.locator('.paper-title').filter(has_text='UI 同步进来的论文').wait_for()
        assert len(client.get('/api/papers').json()) == papers_before + 1
        status = client.get('/api/zotero/status',
                            params={'library_type': 'user', 'library_id': '12345'}).json()
        assert status['cursor']['committed_version'] == 300 and status['links'] == {'linked': 1}
        # The nickname survives the commit on the cursor row, so a later status can say which
        # library it is talking about without being handed the credentials again.
        assert status['cursor']['label'] == 'UI 测试库', status['cursor']
        assert status['cursor']['scope']['collections'] == ['COLL0001'], status['cursor']['scope']
        # The key is used and dropped: the field is cleared once the commit returns, and it was
        # never written to the database.
        assert page.locator('#zotero-form [name=api_key]').input_value() == ''
        with client.app.state.store.db.connect() as con:
            assert ZOTERO_KEY not in '\n'.join(con.iterdump())
        assert ZOTERO_KEY not in json.dumps(status, ensure_ascii=False)
        # A second sync over an unchanged library duplicates nothing.
        page.locator('#zotero-form [name=api_key]').fill(ZOTERO_KEY)
        page.locator('#zotero-preview').click()
        page.wait_for_function("document.querySelector('#zotero-result').textContent.includes('游标 300 → 300')")
        assert '无变化' in page.locator('#zotero-result').inner_text()
        page.screenshot(path=str(output_dir/'library-zotero-nochange.png'), full_page=True)
        page.keyboard.press('Escape')
        completed.append("zotero_readonly_sync_preview_then_commit")
        papers=client.get('/api/papers').json()
        actual=next(p for p in papers if p['title']==title)
        assert actual['notes']=='Edited note: durable research context.'
        assert actual['status']=='baseline'
        assert actual['resources'][0]['latest']['status']=='unsupported'
        exported=client.get('/api/export').json()
        assert exported['papers']
        completed.append("persistent_data_and_export")
        # Remove only temporary real entries before demo screenshots.
        for p in papers:
            if not p['is_demo']:
                response=client.delete('/api/papers/'+p['id'],headers={'X-Re0-Client':'web','Content-Type':'application/json'})
                assert response.status_code==204
        page.evaluate('window.re0Reload()')
        page.wait_for_function('document.querySelectorAll(".paper-card").length === 6')
        page.set_viewport_size({'width':390,'height':844})
        page.screenshot(path=str(output_dir/'mobile.png'),full_page=True)
        assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth')
        page.locator('.paper-title').first.click()
        page.screenshot(path=str(output_dir/'mobile-detail.png'),full_page=False)
        assert page.locator('.drawer').bounding_box()['width'] <= 390
        completed.append("mobile_layout_and_detail")
        assert not errors, errors
        # 先落盘/打印报告再关闭浏览器:个别环境在浏览器进程回收阶段会中断,结果不应丢失。
        report={'mode':'offline Chromium DOM + real FastAPI TestClient bridge','browser_network_tested':False,'external_network_tested':False,'completed':completed,'page_errors':errors}
        (output_dir/'browser-report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2), encoding="utf-8")
        print(json.dumps(report,ensure_ascii=False,indent=2))
        browser.close()


if __name__=='__main__':
    output=Path(sys.argv[1]) if len(sys.argv)>1 else ROOT/'test-results/browser'
    run(output)
