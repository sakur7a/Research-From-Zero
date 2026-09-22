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

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'backend'))
sys.path.insert(0,str(ROOT/'backend/tests'))
import httpx
from fastapi.testclient import TestClient
from playwright.sync_api import sync_playwright
from re0.main import create_app
from test_agent import FixtureNetwork, CONFIG


def run(output):
    output.mkdir(parents=True,exist_ok=True)
    checked,errors=[],[]
    for key in ['RE0_LLM_MODEL','RE0_LLM_API_KEY','RE0_LLM_BASE_URL','TAVILY_API_KEY']:
        os.environ.pop(key,None)
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp, TestClient(create_app(str(Path(temp)/'browser.sqlite3'),httpx.MockTransport(FixtureNetwork()))) as client, sync_playwright() as engine:
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
        page.locator('[data-action=test-model]').click()
        page.wait_for_function("document.querySelector('#notice').textContent.includes('测试通过')")
        page.locator('[data-action=close-settings]').click()
        checked.append('model_configuration_secret_not_returned_and_tool_protocol_test')
        # Budgets and library permission live in settings, not in the task form.
        assert page.locator('#task-form details.budget').count()==0
        assert '文献库未授权' in page.locator('.budget-link').inner_text()
        page.locator('.budget-link').click()
        page.locator('#defaults-form').wait_for()
        assert page.locator('#defaults-form [name=max_model_calls]').input_value()=='12'
        page.locator('#defaults-form [name=max_tool_calls]').fill('14')
        page.locator('#defaults-form [name=attempt_seconds]').fill('180')
        page.locator('#defaults-form [name=use_library]').check()
        page.screenshot(path=str(output/'agent-settings-panel.png'),full_page=True)
        page.locator('#defaults-form [type=submit]').click()
        page.wait_for_function("!document.querySelector('#settings').open")
        saved=client.get('/api/agent/config').json()['task_defaults']
        assert saved=={'max_model_calls':12,'max_tool_calls':14,'attempt_seconds':180,'use_library':True},saved
        summary=page.locator('.budget-link').inner_text()
        assert '工具 14 次' in summary and '单次 180 秒' in summary and '文献库已授权' in summary,summary
        # Enabling the library must be stated in the per-task consent, not only in settings.
        assert '本地文献库的书目与摘要' in page.locator('[name=consent_to_send]').locator('xpath=..').inner_text()
        page.screenshot(path=str(output/'agent-settings-defaults.png'),full_page=True)
        checked.append('task_budgets_and_library_permission_saved_from_settings')
        page.locator('[data-example="0"]').click()
        page.locator('[name=consent_to_send]').check()
        page.locator('#task-form [type=submit]').click()
        page.wait_for_function("document.querySelector('.status')?.textContent==='报告已生成'",timeout=15000)
        assert page.locator('.trace-row').count()>5
        checked.append('real_runtime_model_tool_observation_loop_fixture_network')
        rid=client.get('/api/agent/runs').json()[0]['id']
        inherited=client.get('/api/agent/runs/'+rid).json()['params']
        assert (inherited['max_model_calls'],inherited['max_tool_calls'],inherited['attempt_seconds'],inherited['use_library'])==(12,14,180,True),inherited
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
        page.get_by_role('tab',name='研究报告').click()
        page.screenshot(path=str(output/'agent-report-fixture.png'),full_page=True)
        page.set_viewport_size({'width':390,'height':844})
        assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth')
        page.locator('[data-action=history]').click()
        assert page.locator('#history').is_visible()
        page.locator('[data-action=history]').click()
        page.screenshot(path=str(output/'agent-mobile-fixture.png'),full_page=True)
        checked.append('mobile_layout_no_horizontal_overflow_and_history_access')
        page.locator('[data-action=new]').click()
        page.get_by_role('heading',name=re.compile('从一个问题')).wait_for()
        page.screenshot(path=str(output/'agent-home-mobile.png'),full_page=True)
        assert not errors,errors
        # 先落盘/打印报告再关闭浏览器:个别环境在浏览器进程回收阶段会中断,结果不应丢失。
        result={'mode':'offline Chromium + real TestClient + fixture HTTP; no live model','passed':checked,'browser_errors':errors}
        (output/'agent-browser-report.json').write_text(json.dumps(result,indent=2,ensure_ascii=False), encoding="utf-8")
        print(json.dumps(result,ensure_ascii=False,indent=2))
        browser.close()


if __name__=='__main__':
    run(Path(sys.argv[1]) if len(sys.argv)>1 else ROOT/'.data/agent-browser')
