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
from re0.workspace import Workspace
from test_fulltext import read as read_fulltext_fixture
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
        page.locator('.knowledge-relations').wait_for()
        with client.app.state.store.db.connect() as con:
            relation_paper=con.execute("SELECT p.id FROM papers p JOIN resources r ON r.paper_id=p.id "
                                        "JOIN observations o ON o.resource_id=r.id "
                                        "WHERE p.is_demo=1 GROUP BY p.id ORDER BY p.id LIMIT 1").fetchone()
        relation_paper_id=relation_paper['id']
        page.locator('#relation-paper').select_option(relation_paper_id)
        page.locator('[data-action="new-relation"]').click()
        relation_form=page.locator('#relation-form')
        assert relation_form.locator('[name=relation_type] option[value=claim]').get_attribute('disabled') is not None
        relation_form.locator('[name=relation_type]').select_option('uses_method')
        relation_form.locator('[name=target]').fill('扩散模型')
        relation_form.locator('[name=statement]').fill('演示关系；只用于测试来源和版本投影。')
        relation_form.locator('[name=locator]').fill('虚构演示检查记录')
        relation_form.locator('[name=source_snapshot_id]').select_option(index=1)
        relation_form.locator('[type=submit]').click()
        page.locator('.relation-table tbody tr').first.wait_for()
        assert '演示关系' in page.locator('.relation-table').inner_text()
        page.locator('#relation-query').fill('扩散模型')
        page.locator('[data-action="search-relations"]').click()
        page.locator('.relation-table tbody tr').first.wait_for()
        assert page.locator('.relation-table tbody tr').count()==1
        page.locator('.relation-table [data-action="new-relation"]').click()
        revised=page.locator('#relation-form')
        assert revised.locator('[name=supersedes_id]').input_value()
        assert revised.locator('[name=source_snapshot_id]').input_value()
        revised.locator('[name=statement]').fill('演示修订：保留旧行。')
        revised.locator('[name=locator]').fill('虚构演示的复核记录')
        revised.locator('[type=submit]').click()
        try:
            page.wait_for_function("() => !document.querySelector('#relation-form') || "
                                   "Boolean(document.querySelector('#relation-form .form-error')?.textContent)",
                                   timeout=8000)
        except Exception as exc:
            invalid=page.locator('#relation-form :invalid').evaluate_all(
                "nodes=>nodes.map(node=>({name:node.name,value:node.value}))")
            raise AssertionError(f"relation submit stalled: invalid={invalid}; "
                                 f"form={revised.inner_text()}; requests={requests[-4:]}") from exc
        if page.locator('#relation-form').count():
            raise AssertionError('结构化关系修订失败：'+revised.locator('.form-error').inner_text())
        page.locator('.relation-table').get_by_text('演示修订').wait_for()
        assert '演示修订' in page.locator('.relation-table').inner_text()
        assert page.locator('.relation-table tbody tr').count()==2
        assert page.locator('#toasts .toast').count()==1
        completed.append("typed_relations_require_sources_and_revisions_are_append_only")

        page.locator('#relation-query').fill('')
        page.locator('[data-action="search-relations"]').click()
        page.wait_for_function("document.querySelector('.relation-count')?.textContent.includes('匹配 2 条')")

        # Read a fixture full text into a named workspace, import that bundle, then use the page's
        # preview/confirm flow to bind the reviewed chunks to the exact PaperVersion.
        fulltext_paper=client.post('/api/papers',headers={'X-Re0-Client':'web'},json={
            'title':'Full-text import smoke fixture','arxiv_id':'2601.12345v1','version_label':'v1'})
        assert fulltext_paper.status_code==201,fulltext_paper.text
        fulltext_paper_id=fulltext_paper.json()['id']
        source_workspace=Workspace(Path(temp)/'fulltext-workspace').open()
        fulltext_result=read_fulltext_fixture('2601.12345v1',workspace=source_workspace)
        assert fulltext_result['stored']['chunks']>0
        source_bundle=source_workspace.bundle()
        imported_bundle=client.post('/api/workspaces/import',headers={'X-Re0-Client':'web'},
                                    json={'bundle':source_bundle})
        assert imported_bundle.status_code==201,imported_bundle.text
        workspace_rows=client.get('/api/workspaces').json()['workspaces']
        listed_fulltext=[source for row in workspace_rows for source in row['sources']
                         if source['kind']=='fulltext_chunk' and source.get('fulltext')]
        assert listed_fulltext, json.dumps(workspace_rows,ensure_ascii=False)
        assert all(source.get('source_url') and source['fulltext'].get('identifier')
                   for source in listed_fulltext), json.dumps(listed_fulltext,ensure_ascii=False)
        page.evaluate('window.re0Reload()')
        page.locator('nav [data-nav="graph"]').click()
        page.locator('#relation-paper').select_option(fulltext_paper_id)
        page.locator('.relation-tools [data-action="new-relation"]').click()
        try:
            page.locator('#fulltext-document').wait_for(timeout=5000)
        except Exception as exc:
            raise AssertionError(f"全文导入面板未渲染：{page.locator('dialog').all_text_contents()}；"
                                 f"selected={page.locator('#relation-paper').input_value()}；"
                                 f"body={page.locator('body').inner_text()[-1000:]}；requests={requests[-6:]}；"
                                 f"pageerrors={errors}；sources={json.dumps(listed_fulltext,ensure_ascii=False)}") from exc
        page.locator('#fulltext-preview').click()
        page.locator('#fulltext-confirm').wait_for()
        assert '未经加密认证' in page.locator('#fulltext-import-result').inner_text()
        assert not any(item['snapshot_kind']=='fulltext_chunk'
                       for item in client.get(f'/api/papers/{fulltext_paper_id}/knowledge').json()['source_snapshots'])
        page.locator('#fulltext-confirm').click()
        fulltext_knowledge=client.get(f'/api/papers/{fulltext_paper_id}/knowledge').json()
        imported_snapshot=next(item for item in fulltext_knowledge['source_snapshots']
                               if item['snapshot_kind']=='fulltext_chunk')
        assert imported_snapshot['paper_version_id']
        assert imported_snapshot['payload']['provenance_verified'] is False
        source_option=page.locator(f'#relation-form [name=source_snapshot_id] option[value="{imported_snapshot["id"]}"]')
        try:
            source_option.wait_for(state='attached',timeout=5000)
        except Exception as exc:
            raise AssertionError('全文已落库但关系窗口未刷新：'
                                 +page.locator('#fulltext-import-result').inner_text()) from exc
        relation_form=page.locator('#relation-form')
        assert relation_form.locator('[name=relation_type] option[value=claim]').get_attribute('disabled') is None, \
            json.dumps({'snapshots':fulltext_knowledge['source_snapshots'],
                        'select':relation_form.locator('[name=relation_type]').inner_html()},ensure_ascii=False)
        relation_form.locator('[name=relation_type]').select_option('claim')
        relation_form.locator('[name=assertion_kind]').select_option('human_confirmation')
        relation_form.locator('[name=target]').fill('Fixture claim target')
        relation_form.locator('[name=statement]').fill('已核对导入的全文块；只用于本机 smoke。')
        relation_form.locator('[name=conditions]').fill('仅适用于引用的 v1 段落。')
        relation_form.locator('[name=source_snapshot_id]').select_option(imported_snapshot['id'])
        stored_chunk=source_workspace.read(source_workspace.identifiers()[0])
        locator=re.search(r'^\[([^\]]+)\]',stored_chunk['content'],re.M).group(1)
        relation_form.locator('[name=locator]').fill(locator)
        relation_form.locator('[type=submit]').click()
        try:
            page.locator('.relation-table').get_by_text('Fixture claim target').wait_for(timeout=8000)
        except Exception as exc:
            errors_text=(relation_form.locator('.form-error').text_content()
                         if relation_form.count() and relation_form.locator('.form-error').count() else '')
            raise AssertionError('全文 claim 提交失败：'+errors_text+f"；requests={requests[-5:]}") from exc
        assert client.get(f'/api/papers/{fulltext_paper_id}/knowledge').json()['relations'][0]['paper_version_id']==imported_snapshot['paper_version_id']
        assert client.delete(f'/api/papers/{fulltext_paper_id}',headers={
            'X-Re0-Client':'web','Content-Type':'application/json'}).status_code==204
        page.evaluate('window.re0Reload()')
        page.locator('nav [data-nav="graph"]').click()
        completed.append("fulltext_bundle_preview_confirm_version_binding_and_manual_claim")
        page.screenshot(path=str(output_dir/"graph.png"),full_page=True)
        page.locator('#search').fill('no matching paper')
        assert page.locator('.graph-paper').count()==0
        assert page.locator('.knowledge-relations').count()==1
        assert '结构化关系与主张' in page.locator('.knowledge-relations').inner_text()
        page.locator('#search').fill('')
        assert page.locator('.graph-paper').count()==6
        completed.append("relation_index_remains_available_when_graph_filter_is_empty")
        page.locator('nav [data-nav="library"]').click()
        completed.append("graph_separates_topic_projection_from_evidence_relations")
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
        assert '记录类型：来源观察' in page.locator('#evidence-content').inner_text()
        page.locator('#revise-audit').click()
        review=page.locator('#audit-confirmation-form')
        review.locator('[name=attribution]').select_option('official')
        review.locator('[name=review_note]').fill('已核对论文页面的资源链接；访问状态仍按原观察记录。')
        review.locator('[type=submit]').click()
        page.wait_for_function("Boolean(document.querySelector('#audit-confirmation-form .form-error')?.textContent)")
        assert '依据' in review.locator('.form-error').inner_text()
        assert not any('/confirmations' in path for _,path in requests)
        review.locator('[name=attribution_source_url]').fill('https://arxiv.org/abs/2401.00001')
        review.locator('[name=attribution_excerpt]').fill('论文作者在论文页面明确链接到该资源。')
        review.locator('[type=submit]').click()
        page.wait_for_function("() => !document.querySelector('#audit-confirmation-form') || "
                               "Boolean(document.querySelector('#audit-confirmation-form .form-error')?.textContent)")
        if page.locator('#audit-confirmation-form').count():
            raise AssertionError('人工复核未提交：'+page.locator('#audit-confirmation-form .form-error').inner_text())
        page.locator('.record-provenance').wait_for()
        assert '人工复核' in page.locator('.record-provenance').inner_text()
        assert page.locator('#history-select option').count()==2
        review_paper=next(p for p in client.get('/api/papers').json() if p['title']==title)
        resource_id=review_paper['resources'][0]['id']
        confirmed=client.get(f"/api/resources/{resource_id}/observations").json()
        assert confirmed[0]['record_kind']=='confirmation'
        assert confirmed[0]['attribution']=='official'
        assert confirmed[1]['record_kind']=='observation'
        completed.append("append_human_resource_revision_without_replacing_observation")
        page.keyboard.press('Escape')
        page.keyboard.press('Escape')
        completed.append("edit_notes_and_record_unsupported_check")
        # Import via the actual file-picker workflow and real backend preview.
        write_headers={'X-Re0-Client':'web'}
        doi_conflict=client.post('/api/papers',headers=write_headers,json={"title":"Existing DOI work","doi":"10.1234/ui-existing"}).json()
        arxiv_conflict=client.post('/api/papers',headers=write_headers,json={"title":"Existing arXiv work","arxiv_id":"2401.12345v1"}).json()
        conflict_item={"title":"Conflicting DOI and arXiv","DOI":"10.1234/ui-existing",
                       "URL":"https://arxiv.org/abs/2401.12345v1"}
        page.locator('.page-actions [data-action="import"]').click()
        content=json.dumps([
            {"title":"Same title, distinct DOI","DOI":"10.1234/ui-csl-one"},
            {"title":"Same title, distinct DOI","DOI":"10.1234/ui-csl-two"},
            conflict_item,
        ])
        page.locator('#import-file').set_input_files({"name":"library.json","mimeType":"application/json","buffer":content.encode()})
        page.locator('#confirm-import').wait_for()
        preview_text=page.locator('#import-preview').inner_text()
        assert '身份冲突 · 不会自动合并' in preview_text and '分别指向不同文献' in preview_text, preview_text
        assert '精确重复的 DOI / arXiv 会跳过' in page.locator('dialog.modal').inner_text()
        assert '同标题但标识符不同的论文会分别保留' in page.locator('dialog.modal').inner_text()
        page.screenshot(path=str(output_dir/'csl-import-preview.png'),full_page=True)
        page.set_viewport_size({'width':390,'height':844})
        page.screenshot(path=str(output_dir/'csl-import-preview-mobile.png'),full_page=False)
        assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth')
        page.locator('dialog.modal').evaluate('(dialog)=>dialog.scrollTop=dialog.scrollHeight')
        mobile_confirm=page.locator('#confirm-import').bounding_box()
        assert mobile_confirm['y'] >= 0 and mobile_confirm['y']+mobile_confirm['height'] <= 844
        page.set_viewport_size({'width':1440,'height':1050})
        page.locator('#confirm-import').wait_for()
        assert '导入 2 篇并记录 1 条冲突' in page.locator('#confirm-import').inner_text()
        page.locator('#confirm-import').click()
        page.locator('.paper-title').filter(has_text='Same title, distinct DOI').first.wait_for()
        assert page.locator('.paper-title').filter(has_text='Same title, distinct DOI').count()==2
        assert any(row['snapshot_kind']=='identity_conflict' for row in
                   client.get(f"/api/papers/{doi_conflict['id']}/knowledge").json()['source_snapshots'])
        assert any(row['snapshot_kind']=='identity_conflict' for row in
                   client.get(f"/api/papers/{arxiv_conflict['id']}/knowledge").json()['source_snapshots'])
        page.locator('.page-actions [data-action="import"]').click()
        page.locator('#import-file').set_input_files({"name":"conflict-only.json","mimeType":"application/json",
                                                      "buffer":json.dumps([conflict_item]).encode()})
        page.locator('#confirm-import').wait_for()
        assert '记录 1 条待复核冲突' in page.locator('#confirm-import').inner_text()
        assert not page.locator('#confirm-import').is_disabled()
        page.locator('#confirm-import').click()
        page.locator('dialog.modal').wait_for(state='detached')
        page.wait_for_function("document.querySelector('#toasts')?.innerText.includes('身份待复核 1 条')")
        for conflict_paper in (doi_conflict, arxiv_conflict):
            recorded=client.get(f"/api/papers/{conflict_paper['id']}/knowledge").json()['source_snapshots']
            assert sum(row['snapshot_kind'] in {'identifier_declaration','identity_conflict'}
                       for row in recorded)==2, json.dumps(recorded,ensure_ascii=False)
        completed.append("csl_distinct_titles_and_persisted_identity_conflict")
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
