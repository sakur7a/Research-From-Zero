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
    with tempfile.TemporaryDirectory() as temp, TestClient(create_app(str(Path(temp)/'browser.sqlite3'),httpx.MockTransport(FixtureNetwork()))) as client, sync_playwright() as engine:
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
        html=(ROOT/'web/agent.html').read_text()
        html=re.sub(r'<link[^>]+>','',html)
        html=re.sub(r'<script[^>]*>.*?</script>','',html,flags=re.S)
        html=html.replace('</head>','<style>'+(ROOT/'web/agent.css').read_text()+'</style></head>')
        page.set_content(html)
        page.evaluate('''()=>{window.fetch=async(path,options={})=>{const r=await window.re0TestRequest(path,{method:options.method||'GET',headers:options.headers||{},body:options.body});return new Response(r.body,{status:r.status,headers:{'Content-Type':'application/json'}});};}''')
        js=[]
        for name in ['core.js','agent-core.js','agent.js']:
            text=(ROOT/'web'/name).read_text()
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
        page.locator('#model-form [type=submit]').click()
        page.wait_for_function("!document.querySelector('#settings').open")
        page.locator('.topbar [data-action=settings]').click()
        assert page.locator('[name=api_key]').input_value()==''
        page.locator('[data-action=test-model]').click()
        page.wait_for_function("document.querySelector('#notice').textContent.includes('测试通过')")
        page.locator('[data-action=close-settings]').click()
        checked.append('model_configuration_secret_not_returned_and_tool_protocol_test')
        page.locator('[data-example="0"]').click()
        page.locator('[name=consent_to_send]').check()
        page.locator('#task-form [type=submit]').click()
        page.wait_for_function("document.querySelector('.status')?.textContent==='报告已生成'",timeout=15000)
        assert page.locator('.trace-row').count()>5
        checked.append('real_runtime_model_tool_observation_loop_fixture_network')
        page.get_by_role('tab',name='研究报告').click()
        page.get_by_role('heading',name='Fixture report').wait_for()
        assert page.locator('.citations button').count()==1
        page.locator('.citations button').click()
        page.get_by_role('heading',name='Fixture Layout Paper').wait_for()
        page.locator('.evidence-card summary').click()
        assert 'TEST FIXTURE' in page.locator('.evidence-card pre').inner_text()
        checked.append('report_citations_open_real_task_evidence')
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
        browser.close()
    result={'mode':'offline Chromium + real TestClient + fixture HTTP; no live model','passed':checked,'browser_errors':errors}
    (output/'agent-browser-report.json').write_text(json.dumps(result,indent=2,ensure_ascii=False))
    print(json.dumps(result,ensure_ascii=False,indent=2))


if __name__=='__main__':
    run(Path(sys.argv[1]) if len(sys.argv)>1 else ROOT/'.data/agent-browser')
