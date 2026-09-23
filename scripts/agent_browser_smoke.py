"""Offline Chromium UI + real TestClient + fixture model/provider HTTP.

The fixture is explicitly synthetic. This does NOT evaluate real model quality,
real external APIs or browser TCP. No API keys or private papers are used.
"""
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import time

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'backend'))
sys.path.insert(0,str(ROOT/'backend/tests'))
import httpx
from fastapi.testclient import TestClient
from playwright.sync_api import sync_playwright
from re0.main import create_app
from re0.workspace import Workspace
from test_agent import FixtureNetwork, CONFIG
from test_agent_matrix import MatrixFixture, CONFIG as MATRIX_CONFIG, GOAL as MATRIX_GOAL, HEADERS


def run(output):
    output.mkdir(parents=True,exist_ok=True)
    checked,errors=[],[]
    for key in ['RE0_LLM_MODEL','RE0_LLM_API_KEY','RE0_LLM_BASE_URL','TAVILY_API_KEY']:
        os.environ.pop(key,None)
    fixture=FixtureNetwork()
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp, TestClient(create_app(str(Path(temp)/'browser.sqlite3'),httpx.MockTransport(fixture))) as client, sync_playwright() as engine:
        opts={'headless':True}
        if os.getenv('CHROMIUM_PATH'):opts['executable_path']=os.environ['CHROMIUM_PATH']
        browser=engine.chromium.launch(**opts)
        page=browser.new_page(viewport={'width':1440,'height':1080},device_scale_factor=1)
        page.on('pageerror',lambda error:errors.append(str(error)))
        page.set_default_timeout(8000)
        page.on('dialog',lambda dialog:dialog.accept())
        def bridge(path, options):
            assert path.startswith('/api/'),path
            response=client.request(options.get('method','GET'),path,headers=options.get('headers'),content=options.get('body'))
            return {'status':response.status_code,'body':response.text}
        page.expose_function('re0TestRequest',bridge)
        html=(ROOT/'web/agent.html').read_text(encoding="utf-8")
        html=re.sub(r'<link[^>]+>','',html)
        html=re.sub(r'<script[^>]*>.*?</script>','',html,flags=re.S)
        html=html.replace('</head>','<style>'+(ROOT/'web/theme.css').read_text(encoding="utf-8")+'\n'+(ROOT/'web/agent.css').read_text(encoding="utf-8")+'</style></head>')
        page.set_content(html)
        page.evaluate('''()=>{window.fetch=async(path,options={})=>{const r=await window.re0TestRequest(path,{method:options.method||'GET',headers:options.headers||{},body:options.body});return new Response(r.body,{status:r.status,headers:{'Content-Type':'application/json'}});};}''')
        js=[]
        for name in ['core.js','agent-core.js','theme.js','agent.js']:
            text=(ROOT/'web'/name).read_text(encoding="utf-8")
            text=re.sub(r'^import .*?;\n','',text,flags=re.M)
            text=re.sub(r'\bexport (?=(?:async )?(?:function|const|let|class))','',text)
            js.append(text)
        page.add_script_tag(content='\n'.join(js),type='module')
        page.get_by_role('heading',name=re.compile('从一个问题')).wait_for()
        assert page.locator('#task-form [type=submit]').is_disabled()
        page.screenshot(path=str(output/'agent-home.png'),full_page=True)
        checked.append('agent_home_no_mock_statistics_and_model_required')
        page.locator('.setup-note [data-action=settings]').click()
        page.locator('#settings').wait_for()
        assert '最多 8 小时' in page.locator('#settings').inner_text()
        page.locator('[name=base_url]').fill(CONFIG['base_url'])
        page.locator('[name=model]').fill(CONFIG['model'])
        page.locator('[name=api_key]').fill(CONFIG['api_key'])
        page.locator('[name=trust_endpoint]').check()
        # The picker asks the endpoint what it serves; the key rides along once.
        page.locator('[data-action=fetch-models]').click()
        page.wait_for_function("document.querySelectorAll('#model-options option').length===2")
        assert page.locator('#model-options option[value="fixture-model-pro"]').count()==1
        # Regression guard: a toast left in <body> paints *underneath* the modal
        # dialog's blurred ::backdrop and looks invisible. It must live inside the
        # open dialog, and nothing may cover it.
        assert page.evaluate("document.querySelector('#settings').contains(document.querySelector('#notice'))")
        box=page.locator('#notice').bounding_box()
        assert box, 'notice has no layout box'
        assert page.evaluate("([x,y])=>{const el=document.elementFromPoint(x,y);return Boolean(el&&el.closest('#notice'));}",
                             [box['x']+box['width']/2,box['y']+box['height']/2]), 'notice is covered by another layer'
        page.screenshot(path=str(output/'agent-settings-model-picker.png'),full_page=True)
        checked.append('model_picker_fills_candidates_and_its_toast_is_not_hidden_by_the_modal')
        page.locator('#model-form [type=submit]').click()
        page.wait_for_function("!document.querySelector('#settings').open")
        page.locator('.topbar [data-action=settings]').click()
        assert page.locator('[name=api_key]').input_value()==''
        saved_config=client.get('/api/agent/config').json()
        assert saved_config['configured'] and saved_config['credential_expires_at'], saved_config
        connection_tests=[request for request in fixture.requests
                          if request.url.path.endswith('/chat/completions')
                          and isinstance(json.loads(request.content).get('tool_choice'),dict)]
        assert len(connection_tests)==1, f'connect-and-save must send one connection test: {len(connection_tests)}'
        settings_text=page.locator('#settings').inner_text()
        assert '当前配置最晚有效至' in settings_text, settings_text
        page.locator('[data-action=close-settings]').click()
        checked.append('one_click_connection_test_saves_only_after_success_without_returning_key')
        # Budgets and library permission live in settings, not in the task form.
        assert page.locator('#task-form details.budget').count()==0
        assert '文献库未授权' in page.locator('.budget-link').inner_text()
        page.locator('.budget-link').click()
        page.locator('.defaults-block > summary').click()
        page.locator('#defaults-form').wait_for()
        assert page.locator('#defaults-form [name=max_model_calls]').input_value()=='12'
        page.locator('#defaults-form [name=max_tool_calls]').fill('14')
        page.locator('#defaults-form [name=attempt_seconds]').fill('180')
        page.locator('#defaults-form [name=research_scope]').select_option('expanded')
        page.locator('#defaults-form [name=use_library]').check()
        page.screenshot(path=str(output/'agent-settings-panel.png'),full_page=True)
        page.locator('#defaults-form [type=submit]').click()
        page.wait_for_function("!document.querySelector('#settings').open")
        saved=client.get('/api/agent/config').json()['task_defaults']
        assert saved=={'max_model_calls':12,'max_tool_calls':14,'max_upstream_requests':60,'attempt_seconds':180,'use_library':True,'research_scope':'expanded'},saved
        summary=page.locator('.budget-link').inner_text()
        assert '工具 14 次' in summary and '较宽起步' in summary and '单次 180 秒' in summary and '文献库已授权' in summary,summary
        # Enabling the library must be stated in the per-task consent, not only in settings.
        assert '本地文献库的书目与摘要' in page.locator('[name=consent_to_send]').locator('xpath=..').inner_text()
        page.screenshot(path=str(output/'agent-settings-defaults.png'),full_page=True)
        checked.append('task_budgets_and_library_permission_saved_from_settings')
        page.locator('[data-example="0"]').click()
        assert page.locator('#task-form [name=research_scope]').input_value()=='expanded'
        page.locator('#task-form [name=research_scope]').select_option('focused')
        page.locator('[name=consent_to_send]').check()
        page.locator('#task-form [type=submit]').click()
        page.wait_for_function("document.querySelector('.status')?.textContent==='报告已生成'",timeout=15000)
        assert page.locator('[data-tab="report"][aria-selected="true"]').count()==1
        page.locator('[data-tab="trace"]').click()
        assert page.locator('.trace-row').count()>5
        checked.append('real_runtime_model_tool_observation_loop_fixture_network')
        rid=client.get('/api/agent/runs').json()[0]['id']
        inherited=client.get('/api/agent/runs/'+rid).json()['params']
        assert (inherited['max_model_calls'],inherited['max_tool_calls'],inherited['max_upstream_requests'],inherited['attempt_seconds'],inherited['use_library'])==(12,14,60,180,True),inherited
        assert inherited['research_scope']=='focused',inherited
        checked.append('new_task_inherits_saved_workspace_defaults')
        page.get_by_role('tab',name='研究报告').click()
        page.get_by_role('heading',name='Fixture report').wait_for()
        assert page.locator('.citations button').count()==1
        page.locator('.citations button').click()
        page.get_by_role('heading',name='Fixture Layout Paper').wait_for()
        page.locator('.evidence-card summary').click()
        assert 'TEST FIXTURE' in page.locator('.evidence-card pre').inner_text()
        checked.append('report_citations_open_real_task_evidence')
        # A paper card is a two-column layout, so pin both columns and the honest statement
        # when no open-source search ran. A missing right column would look like a design
        # choice rather than a regression.
        assert page.locator('.evidence-card.paper-card').count()==1
        assert page.locator('.paper-card .paper-title').inner_text().strip()=='Fixture Layout Paper'
        assert page.locator('.paper-card .paper-aside').is_visible()
        assert page.locator('.paper-card .paper-links a').count()>=1
        aside=page.locator('.paper-card .paper-aside').inner_text()
        assert '未做开源检索' in aside and '没有找到开源候选' in aside, aside
        assert page.locator('.paper-state').inner_text().strip()!='', 'a state chip must name the state'
        page.screenshot(path=str(output/'agent-evidence-paper-card.png'),full_page=True)
        checked.append('paper_evidence_renders_as_a_two_column_card')
        page.locator('[data-import]').click()
        page.wait_for_function("document.querySelector('#notice').textContent.includes('已加入文献库')")
        assert len(client.get('/api/papers').json())==1
        page.locator('[data-import]').click()
        page.wait_for_function("document.querySelector('#notice').textContent.includes('已有这篇论文')")
        assert len(client.get('/api/papers').json())==1
        checked.append('human_approval_idempotent_import')
        # A continuing turn (issue #9). The composer stays collapsed until asked for, offers this
        # run's own evidence for reuse, and its checkboxes are exactly what the request carries.
        assert page.locator('.followup').count()==1
        assert not page.evaluate("document.querySelector('.followup').open")
        page.locator('.followup summary').click()
        assert page.evaluate("document.querySelector('.followup').open")
        assert page.locator('[data-action=workspace-import-confirm]').is_hidden()
        assert page.locator('[data-action=workspace-export]').is_disabled()
        assert '会话累计' in page.locator('.followup .ledger-note').inner_text()
        # Both consent gates are required attributes, so the form itself stops an unauthorized submit.
        assert page.locator('#followup-form [name=goal]').get_attribute('required') is not None
        assert page.locator('#followup-form [name=authorize]').get_attribute('required') is not None
        page.locator('#followup-form [name=goal]').fill('只保留有训练代码的两篇，并补查它们的数据划分')
        page.locator('#followup-form [name=research_scope]').select_option('expanded')
        page.locator('#followup-form .reuse input[type=checkbox]').first.check()
        page.locator('#followup-form [name=authorize]').check()
        page.screenshot(path=str(output/'agent-followup-composer.png'),full_page=True)
        page.locator('#followup-form [type=submit]').click()
        # Wait on the turn label, not on the status chip alone: run 1 already reads "报告已生成", so a
        # status-only wait passes against the stale DOM before the new turn is rendered. The label
        # alone is not enough either — it appears when the turn starts, and the delta only exists
        # once this turn's report does, so both are required in one snapshot.
        page.wait_for_function("document.querySelector('.run-controls span')?.textContent.includes('第 2 轮 · 追问')",timeout=20000)
        page.wait_for_function("document.querySelector('.run-controls span')?.textContent.includes('第 2 轮 · 追问') && document.querySelector('.status')?.classList.contains('completed')",timeout=30000)
        assert '第 2 轮 · 追问' in page.locator('.run-controls span').first.inner_text()
        history=client.get('/api/agent/runs').json()
        assert len(history)==2,history
        second=[row for row in history if row['turn']==2][0]
        assert second['kind']=='followup'
        assert second['status']=='completed',second
        assert second['conversation_id']==[row for row in history if row['turn']==1][0]['conversation_id']
        assert client.get('/api/agent/runs/'+second['id']).json()['params']['research_scope']=='expanded'
        detail=client.get('/api/agent/runs/'+second['id']).json()
        # Reuse means the material was carried, not fetched again.
        assert any(item.get('reused_from') for item in detail['evidence']),detail['evidence']
        assert detail['report_delta']['against_turn']==1
        assert detail['origin']['permissions']['reuse_count']==1
        page.get_by_role('tab',name='研究报告').click()
        page.get_by_role('heading',name=re.compile('与第 1 轮的差异')).wait_for()
        assert '不表示上一轮结论被推翻' in page.locator('.report-delta').inner_text()
        page.screenshot(path=str(output/'agent-report-delta.png'),full_page=True)
        checked.append('followup_turn_reuses_evidence_and_states_its_delta')
        page.get_by_role('tab',name='研究报告').click()
        page.screenshot(path=str(output/'agent-report-fixture.png'),full_page=True)
        # #4: move one source from the explicit MCP-style workspace bundle into this owner's
        # server-managed workspace, preview it, confirm import, then reuse only that source by ID.
        external=Workspace(output/'external-workspace').open()
        external_source=external.record({"source_url":"https://export.arxiv.org/abs/2501.12345",
                                         "locator":"fixture section 2 paragraph 3","kind":"paper",
                                         "content":"FIXTURE bundle body; not a real paper claim.",
                                         "paper":{"title":"Fixture imported workspace source"}},
                                        tool="search_papers")
        bundle_path=output/'workspace-bundle.json'
        bundle_path.write_text(json.dumps(external.bundle(),ensure_ascii=False),encoding='utf-8')
        assert client.get('/api/workspaces').json()['workspaces']==[]
        page.locator('.followup summary').click()
        page.locator('#workspace-bundle-file').set_input_files(str(bundle_path))
        page.locator('[data-action=workspace-preview]').click()
        page.wait_for_function("document.querySelector('#workspace-import-status')?.textContent.includes('新来源 1')")
        assert client.get('/api/workspaces').json()['workspaces']==[], 'preview must write nothing'
        page.locator('[data-action=workspace-import-confirm]').click()
        page.wait_for_function("document.querySelector('#workspace-choice')?.querySelectorAll('option').length===2")
        page.locator('.followup summary').click()
        page.locator('#workspace-choice').select_option(external.workspace_id)
        assert page.locator('[data-action=workspace-export]').is_enabled()
        with page.expect_download() as pending_download:
            page.locator('[data-action=workspace-export]').click()
        download=pending_download.value
        assert download.suggested_filename==f're0-workspace-{external.workspace_id}.json'
        download.save_as(str(output/'downloaded-workspace-bundle.json'))
        exported=json.loads((output/'downloaded-workspace-bundle.json').read_text(encoding='utf-8'))
        assert exported['workspace_id']==external.workspace_id
        assert len(exported['sources'])==1 and exported['sources'][0]['imported_by_user'] is True
        assert page.locator('.workspace-source').inner_text().find('来源声明未经认证')>=0
        page.screenshot(path=str(output/'agent-workspace-imported.png'),full_page=True)
        page.locator('input[name=workspace_reuse]').check()
        page.locator('#followup-form [name=goal]').fill('只使用导入的来源快照，并说明来源声明仍需复核')
        page.locator('#followup-form [name=authorize]').check()
        page.locator('#followup-form [type=submit]').click()
        page.wait_for_function("document.querySelector('.run-controls span')?.textContent.includes('第 3 轮 · 追问')",timeout=20000)
        page.wait_for_function("document.querySelector('.run-controls span')?.textContent.includes('第 3 轮 · 追问') && document.querySelector('.status')?.classList.contains('completed')",timeout=30000)
        third=[row for row in client.get('/api/agent/runs').json() if row['turn']==3][0]
        third_detail=client.get('/api/agent/runs/'+third['id']).json()
        authorizations=third_detail['origin']['permissions']
        assert authorizations['workspace_id']==external.workspace_id
        assert authorizations['reuse_count']==1
        assert str(external.root) not in json.dumps(third_detail)
        assert any(item.get('reused_from',{}).get('provenance_verified') is False
                   for item in third_detail['evidence'])
        checked.append('workspace_bundle_preview_import_owner_scope_and_followup_reuse')
        page.set_viewport_size({'width':390,'height':844})
        assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth')
        page.locator('.followup summary').click()
        assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth')
        page.screenshot(path=str(output/'agent-mobile-workspace-followup.png'),full_page=True)
        page.locator('.followup summary').click()
        page.locator('[data-action=history]').click()
        assert page.locator('#history').is_visible()
        page.locator('[data-action=history]').click()
        page.screenshot(path=str(output/'agent-mobile-fixture.png'),full_page=True)
        checked.append('mobile_layout_no_horizontal_overflow_and_history_access')
        page.locator('[data-action=new]').click()
        page.get_by_role('heading',name=re.compile('从一个问题')).wait_for()
        page.screenshot(path=str(output/'agent-home-mobile.png'),full_page=True)
        # Second offline browser flow: a model proposes three evidence-bound paper/resource rows,
        # then the reader previews and confirms them through the ordinary workbench UI.
        matrix_fixture=MatrixFixture()
        with TestClient(create_app(str(Path(temp)/'agent-matrix-browser.sqlite3'),
                                   httpx.MockTransport(matrix_fixture)),headers=HEADERS) as matrix_client:
            matrix_page=browser.new_page(viewport={'width':1440,'height':1080},device_scale_factor=1)
            matrix_page.set_default_timeout(10000)
            matrix_page.on('pageerror',lambda error:errors.append('matrix: '+str(error)))
            matrix_page.on('dialog',lambda dialog:dialog.accept())
            def matrix_bridge(path, options):
                assert path.startswith('/api/'),path
                response=matrix_client.request(options.get('method','GET'),path,
                                               headers=options.get('headers'),content=options.get('body'))
                return {'status':response.status_code,'body':response.text}
            matrix_page.expose_function('re0TestRequest',matrix_bridge)
            matrix_page.set_content(html)
            matrix_page.evaluate('''()=>{window.fetch=async(path,options={})=>{const r=await window.re0TestRequest(path,{method:options.method||'GET',headers:options.headers||{},body:options.body});return new Response(r.body,{status:r.status,headers:{'Content-Type':'application/json'}});};}''')
            matrix_page.add_script_tag(content='\n'.join(js),type='module')
            matrix_page.get_by_role('heading',name=re.compile('从一个问题')).wait_for()
            matrix_page.locator('.setup-note [data-action=settings]').click()
            matrix_page.locator('[name=base_url]').fill(MATRIX_CONFIG['base_url'])
            matrix_page.locator('[name=model]').fill(MATRIX_CONFIG['model'])
            matrix_page.locator('[name=api_key]').fill(MATRIX_CONFIG['api_key'])
            matrix_page.locator('[name=trust_endpoint]').check()
            matrix_page.locator('#model-form [type=submit]').click()
            matrix_page.wait_for_function("!document.querySelector('#settings').open")
            matrix_page.locator('#goal').fill(MATRIX_GOAL['goal'])
            matrix_page.locator('[name=consent_to_send]').check()
            matrix_page.locator('#task-form [type=submit]').click()
            matrix_page.wait_for_function("document.querySelector('.status')?.textContent==='报告已生成'",timeout=30000)
            matrix_page.wait_for_selector('[data-tab="matrix"][aria-selected="true"]',timeout=12000)
            assert matrix_page.locator('.matrix-card').count()==4
            assert matrix_page.locator('.matrix-card input[name="matrix-selection"]').count()==3
            assert '待人工确认' in matrix_page.locator('.resource-matrix').inner_text()
            matrix_selections=matrix_page.locator('.matrix-card input[name="matrix-selection"]')
            for index in range(matrix_selections.count()):matrix_selections.nth(index).check()
            matrix_page.locator('[data-action=matrix-preview]').click()
            matrix_page.wait_for_function("document.querySelector('#matrix-import-preview')?.textContent.includes('预览：2 组来源数据')")
            assert matrix_client.get('/api/papers').json()==[]
            matrix_page.locator('[data-action=matrix-confirm]').click()
            matrix_page.wait_for_function("document.querySelector('#matrix-import-preview')?.textContent.includes('已保存：新增 2 篇论文')")
            assert len(matrix_client.get('/api/papers').json())==2
            matrix_page.set_viewport_size({'width':390,'height':844})
            assert matrix_page.evaluate('document.documentElement.scrollWidth <= window.innerWidth')
            matrix_page.screenshot(path=str(output/'agent-resource-matrix-mobile.png'),full_page=True)
            matrix_page.close()
            checked.append('agent_report_to_evidence_linked_resource_matrix_preview_and_batch_approval')

        # Two real browser tabs exercise the one-request progress poll, Retry-After, an offline
        # failure, 401 stop, terminal rendering and the absence of a post-completion poll.
        progress_context=browser.new_context(viewport={'width':1280,'height':900})
        progress_state={'calls':{'tab-a':[],'tab-b':[]},'complete':False}
        progress_run={'id':'fixture-progress-run','created_at':'2026-09-24T00:00:00+00:00',
                      'updated_at':'2026-09-24T00:00:00+00:00','status':'running',
                      'goal':'Fixture progress polling','error':'','conversation_id':'','turn':1,'kind':'new',
                      'config':{'model':'fixture-model'},
                      'params':{'max_model_calls':12,'max_tool_calls':20,'max_upstream_requests':60,
                                'attempt_seconds':360,'research_scope':'focused'},
                      'plan':[],'report':None,'model_calls':1,'tool_calls':0,'upstream_requests':1,
                      'model_request_chars':100,'model_request_bytes':120,'largest_model_request_chars':100,
                      'largest_model_request_bytes':120,'usage':{'total_tokens':14,'unreported_calls':0},
                      'resumes':0,'report_delta':None,'evidence_count':0,'evidence':[]}
        def progress_payload(status):
            run={key:value for key,value in progress_run.items() if key not in {'config','evidence'}}
            run.update({'status':status,'model':'fixture-model'})
            return {'run':run,'events':[],'evidence':[],'next_event_id':0,'evidence_cursor':0,
                    'has_more_events':False,'has_more_evidence':False}
        def progress_bridge(tab_name,path,options):
            if path.startswith('/api/agent/runs/'+progress_run['id']+'/progress'):
                calls=progress_state['calls'][tab_name]
                calls.append(time.monotonic())
                if tab_name=='tab-a' and len(calls)==1:
                    return {'status':429,'body':json.dumps({'detail':'fixture rate limit'}),
                            'headers':{'Content-Type':'application/json','Retry-After':'2'}}
                if tab_name=='tab-a' and len(calls)==2:
                    raise ConnectionError('fixture offline between polls')
                if tab_name=='tab-b' and len(calls)==1:
                    return {'status':401,'body':json.dumps({'detail':'fixture session expired'}),
                            'headers':{'Content-Type':'application/json'}}
                return {'status':200,'body':json.dumps(progress_payload(
                    'completed' if progress_state['complete'] else 'running'),ensure_ascii=False),
                    'headers':{'Content-Type':'application/json'}}
            if path=='/api/auth/session':
                result={'identity':{'kind':'local'},'deployment':{'mode':'local','auth_required':False}}
            elif path=='/api/agent/runs':
                result=[{key:progress_run[key] for key in ('id','created_at','updated_at','status','goal','error','conversation_id','turn','kind')}]
            elif path=='/api/agent/config':
                result={'configured':True,'model':'fixture-model','base_url':'https://api.openai.com/v1',
                        'has_api_key':False,'busy':False,'web_search_enabled':False,
                        'task_defaults':{'max_model_calls':12,'max_tool_calls':20,'max_upstream_requests':60,
                                         'attempt_seconds':360,'use_library':False,'research_scope':'focused'}}
            elif path.endswith('/events'):
                result=[]
            elif path=='/api/workspaces':
                result={'workspaces':[]}
            elif path=='/api/agent/runs/'+progress_run['id']:
                result=progress_run
            else:
                raise AssertionError(f'unexpected progress smoke request: {path}')
            return {'status':200,'body':json.dumps(result,ensure_ascii=False),
                    'headers':{'Content-Type':'application/json'}}
        def bootstrap_progress_page(progress_page,tab_name):
            progress_page.set_default_timeout(8000)
            progress_page.on('pageerror',lambda error:errors.append(f'{tab_name}: '+str(error)))
            progress_page.expose_function('re0TestRequest',lambda path,options:progress_bridge(tab_name,path,options))
            progress_page.set_content(html)
            progress_page.evaluate('''()=>{window.fetch=async(path,options={})=>{const r=await window.re0TestRequest(path,{method:options.method||'GET',headers:options.headers||{},body:options.body});return new Response(r.body,{status:r.status,headers:r.headers||{'Content-Type':'application/json'}});};}''')
            progress_page.add_script_tag(content='\n'.join(js),type='module')
            progress_page.get_by_role('heading',name=re.compile('从一个问题')).wait_for()
            progress_page.locator('#history .history-item').click()
            progress_page.wait_for_function("document.querySelector('.status')?.classList.contains('running')")
        progress_a=progress_context.new_page()
        progress_b=progress_context.new_page()
        bootstrap_progress_page(progress_a,'tab-a')
        bootstrap_progress_page(progress_b,'tab-b')
        progress_a.wait_for_function("document.querySelector('#notice')?.textContent.includes('按服务器要求等待')",timeout=8000)
        progress_b.wait_for_function("document.querySelector('#notice')?.textContent.includes('会话已失效')",timeout=8000)
        assert progress_state['calls']['tab-a'].__len__()==1
        assert progress_state['calls']['tab-b'].__len__()==1
        assert progress_b.url=='about:blank','a progress 401 should stop polling without redirecting this page'
        progress_b.close()
        progress_a.wait_for_function("document.querySelector('#notice')?.textContent.includes('保留当前页面')",timeout=9000)
        assert len(progress_state['calls']['tab-a'])==2
        assert progress_state['calls']['tab-a'][1]-progress_state['calls']['tab-a'][0]>=2
        progress_state['complete']=True
        progress_a.wait_for_function("document.querySelector('.status')?.classList.contains('completed')",timeout=9000)
        call_count=len(progress_state['calls']['tab-a'])
        progress_a.wait_for_timeout(2800)
        assert len(progress_state['calls']['tab-a'])==call_count==3
        progress_a.close();progress_context.close()
        checked.append('two_tab_single_poll_401_stop_429_retry_after_offline_backoff_and_completion_stop')
        assert not errors,errors
        # 先落盘/打印报告再关闭浏览器:个别环境在浏览器进程回收阶段会中断,结果不应丢失。
        result={'mode':'offline Chromium + real TestClient + fixture HTTP; no live model','passed':checked,'browser_errors':errors}
        (output/'agent-browser-report.json').write_text(json.dumps(result,indent=2,ensure_ascii=False), encoding="utf-8")
        print(json.dumps(result,ensure_ascii=False,indent=2))
        browser.close()


if __name__=='__main__':
    run(Path(sys.argv[1]) if len(sys.argv)>1 else ROOT/'.data/agent-browser')
