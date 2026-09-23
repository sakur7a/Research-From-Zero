"""Offline behavioral tests: real gateway/runtime/tools, fixture model and provider HTTP."""
import base64
from concurrent.futures import ThreadPoolExecutor
import json
import threading
import time

import httpx
import pytest
from fastapi.testclient import TestClient
from urllib.parse import urlsplit

from re0.deployment import LOCAL_OWNER
from re0.main import create_app
from re0.models import PaperInput
from re0.agent.model import DEFAULT_HOSTS, LOOPBACK, MODEL_KEY_TTL, ChatModel, ModelError, ModelVault, validate_endpoint
from re0.agent.schemas import ModelConfig, TaskDefaults, TaskInput, PaperSearchArgs, FileArgs, SearchArgs, HubSearchArgs
from re0.agent.tools import ResearchTools, specifications

CONFIG = {"base_url": "https://api.openai.com/v1", "model": "fixture-model", "api_key": "sk-test-do-not-persist", "trust_endpoint": True}
GOAL = {"goal": "查找 layout 论文及资源，保留证据", "consent_to_send": True}
HEADERS = {"X-Re0-Client": "web", "Content-Type": "application/json"}
ATOM = b'''<feed xmlns="http://www.w3.org/2005/Atom"><entry><id>http://arxiv.org/abs/2501.12345v2</id><title>Fixture Layout Paper</title><summary>TEST FIXTURE: a layout study. This is not a real paper claim.</summary><published>2025-01-21T00:00:00Z</published><author><name>Test Author</name></author></entry></feed>'''


def completion(name, args, usage=True):
    result = {"choices": [{"finish_reason": "tool_calls", "message": {"role": "assistant", "content": "", "tool_calls": [
        {"id": "call_fixture_reused", "type": "function", "function": {"name": name, "arguments": json.dumps(args, ensure_ascii=False)}}]}}]}
    if usage:
        result["usage"] = {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}
    return result


def report(ids=None):
    return {"title": "Fixture report", "summary": "基于 fixture 来源整理的测试报告", "outcome": "findings" if ids else "insufficient_evidence",
            "findings": [{"claim": "检索到了摘要；尚未验证训练或权重。", "evidence_ids": ids, "assessment": "uncertain"}] if ids else [],
            "limitations": ["这是离线测试，不是真实模型与外网效果评测。"]}


def wait_done(client, rid):
    # Measured on this machine, even a trivial GET costs 51-193 ms, so a fixture
    # task with several model/tool rounds can need more than 10 s of wall time.
    # The deadline only bounds a failing test; a finishing task returns immediately.
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        run = client.get('/api/agent/runs/' + rid).json()
        if run['status'] not in {'queued', 'running'} and not client.get('/api/agent/config').json()['busy']:
            return run
        time.sleep(.015)
    raise AssertionError('Fixture task did not finish')


def check_protocol(messages):
    pending = set()
    for m in messages:
        if m['role'] == 'assistant':
            assert not pending, 'Model called before all tool responses arrived'
            pending.update(x['id'] for x in m.get('tool_calls', []))
        elif m['role'] == 'tool':
            assert m['tool_call_id'] in pending, 'Tool response lost its assistant call'
            pending.remove(m['tool_call_id'])
        else:
            assert not pending, 'Non-tool message before pending tools were answered'
    assert not pending


class FixtureNetwork:
    def __init__(self, invalid_citation=False):
        self.requests, self.model_count, self.invalid_citation = [], 0, invalid_citation

    def __call__(self, request):
        self.requests.append(request)
        if request.url.host == 'export.arxiv.org':
            return httpx.Response(200, content=ATOM)
        if request.url.path.endswith('/models'):
            assert request.url == 'https://api.openai.com/v1/models'
            assert request.headers['Authorization'] == 'Bearer ' + CONFIG['api_key']
            return httpx.Response(200, json={'data': [{'id': 'fixture-model'}, {'id': 'fixture-model-pro'}, {'id': 'fixture-model'}]})
        assert request.url == 'https://api.openai.com/v1/chat/completions'
        assert request.headers['Authorization'] == 'Bearer ' + CONFIG['api_key']
        data = json.loads(request.content)
        if isinstance(data.get('tool_choice'), dict):
            return httpx.Response(200, json=completion('connection_check', {}))
        check_protocol(data['messages'])
        self.model_count += 1
        if self.model_count == 1:
            return httpx.Response(200, json=completion('update_plan', {'steps':['搜索论文', '阅读资源线索', '整理证据报告']}))
        if self.model_count == 2:
            return httpx.Response(200, json=completion('search_papers', {'query':'layout', 'limit':1, 'source':'arxiv'}))
        if self.model_count == 3 and self.invalid_citation:
            return httpx.Response(200, json=completion('finish_report', report(['invented-source'])))
        evidence = [ev['id'] for m in data['messages'] if m['role'] == 'tool' for ev in json.loads(m['content']).get('evidence', [])]
        return httpx.Response(200, json=completion('finish_report', report(evidence)))


@pytest.fixture(autouse=True)
def isolate_config(monkeypatch):
    for name in ['RE0_LLM_MODEL','RE0_LLM_API_KEY','RE0_LLM_BASE_URL','RE0_LLM_ALLOWED_HOSTS','TAVILY_API_KEY','GITHUB_TOKEN']:
        monkeypatch.delenv(name, raising=False)


def test_requires_model_and_consent(client):
    assert client.post('/api/agent/runs', json=GOAL).status_code == 422
    assert client.post('/api/agent/runs', json={'goal':'test task'}).status_code == 422
    assert client.get('/').status_code == 200
    assert 'agent.js' in client.get('/').text
    assert 'app.js' in client.get('/library').text


def test_config_redaction_and_clearing(client):
    response = client.put('/api/agent/config', json=CONFIG)
    assert response.status_code == 200
    assert CONFIG['api_key'] not in response.text
    assert client.get('/api/health').json()['llm_enabled']
    assert CONFIG['api_key'] not in client.get('/api/agent/config').text
    assert CONFIG['api_key'] not in client.app.state.store.db.path
    with client.app.state.store.db.connect() as con:
        assert CONFIG['api_key'] not in '\n'.join(con.iterdump())
    assert client.request('DELETE', '/api/agent/config', json={}).json()['configured'] is False


def test_validation_error_never_echoes_secret(client):
    secret = 'private-key\ninvalid'
    res = client.put('/api/agent/config', json={**CONFIG, 'api_key': secret})
    assert res.status_code == 422
    assert 'private-key' not in res.text
    res = client.put('/api/agent/config', json={**CONFIG, 'unknown': CONFIG['api_key']})
    assert res.status_code == 422 and CONFIG['api_key'] not in res.text


@pytest.mark.parametrize('url', ['http://api.openai.com/v1', 'https://api.openai.com.evil.example/v1', 'https://user:password@api.openai.com/v1',
    'https://api.openai.com/v1?token=abc','http://169.254.169.254/v1','http://192.168.1.1:1234/v1',
    'https://api.openai.com:999/v1','https://api.openai.com/v1/../admin','https://api.openai.com/v1/%2fadmin','file:///etc/passwd'])
def test_endpoint_boundaries(url):
    with pytest.raises(ModelError):
        validate_endpoint(ModelConfig(**{**CONFIG, 'base_url':url}))


def test_local_endpoint_no_key():
    config=validate_endpoint(ModelConfig(**{**CONFIG, 'base_url':'http://127.0.0.1:11434/v1','api_key':''}))
    assert not config.public()['has_api_key']


def test_config_custom_allowlist(monkeypatch):
    monkeypatch.setenv('RE0_LLM_ALLOWED_HOSTS','llm.example.org')
    assert validate_endpoint(ModelConfig(**{**CONFIG,'base_url':'https://llm.example.org/v1'}))


def test_model_protocol_and_secret_not_echoed():
    def transport(request):
        data=json.loads(request.content)
        assert data['max_completion_tokens'] == 3000
        assert 'max_tokens' not in data
        assert 'temperature' not in data
        return httpx.Response(200,json=completion('connection_check', {}, usage=False))
    model=ChatModel(ModelConfig(**CONFIG,token_parameter='max_completion_tokens'),httpx.MockTransport(transport))
    assert model.test()['tool_calling']


@pytest.mark.parametrize('status',[302,400,401,429,500])
def test_gateway_error_body_redacted(status):
    count=[]
    def transport(request):
        count.append(1)
        return httpx.Response(status,json={'error':CONFIG['api_key']},headers={'location':'https://evil.example/'})
    model=ChatModel(ModelConfig(**CONFIG),httpx.MockTransport(transport))
    with pytest.raises(ModelError) as err:
        model.test()
    assert CONFIG['api_key'] not in str(err.value)
    assert len(count)==1


def test_gateway_rejects_plain_chat_as_tool_test():
    model=ChatModel(ModelConfig(**CONFIG),httpx.MockTransport(lambda _:httpx.Response(200,json={'choices':[{'message':{'content':'hello'},'finish_reason':'stop'}]})))
    with pytest.raises(ModelError):model.test()


def test_end_to_end_tool_loop_and_human_import(tmp_path):
    network=FixtureNetwork()
    with TestClient(create_app(str(tmp_path/'agent.sqlite3'),httpx.MockTransport(network)),headers=HEADERS) as c:
        assert c.put('/api/agent/config',json=CONFIG).status_code==200
        assert c.post('/api/agent/config/test',json={}).json()['ok']
        run=c.post('/api/agent/runs',json=GOAL).json()
        final=wait_done(c,run['id'])
        assert final['status']=='completed',final
        assert final['model_calls']==3 and final['tool_calls']==3
        assert final['usage']['total_tokens']==45
        assert final['report']['findings'][0]['evidence_ids']==[final['evidence'][0]['id']]
        assert final['evidence'][0]['paper']['arxiv_id']=='2501.12345v2'
        assert c.get('/api/papers').json()==[]
        path=f"/api/agent/runs/{run['id']}/evidence/{final['evidence'][0]['id']}/import"
        assert c.post(path,json={'confirmed':False}).status_code==422
        first=c.post(path,json={'confirmed':True}).json()
        second=c.post(path,json={'confirmed':True}).json()
        assert first['created'] and not second['created']
        assert first['paper_id']==second['paper_id']
        assert len(c.get('/api/papers').json())==1
        events=c.get(f"/api/agent/runs/{run['id']}/events").json()
        assert any(x['kind']=='plan' for x in events)
        assert c.get(f"/api/agent/runs/{run['id']}/events?after={events[-1]['id']}").json()==[]
        export=c.get(f"/api/agent/runs/{run['id']}/export").text
        assert CONFIG['api_key'] not in export and 'messages' not in export
        with c.app.state.store.db.connect() as con:
            assert CONFIG['api_key'] not in '\n'.join(con.iterdump())


def test_invalid_citation_gets_tool_error_and_model_can_repair(tmp_path):
    network=FixtureNetwork(invalid_citation=True)
    with TestClient(create_app(str(tmp_path/'repair.sqlite3'),httpx.MockTransport(network)),headers=HEADERS) as c:
        c.put('/api/agent/config',json=CONFIG)
        final=wait_done(c,c.post('/api/agent/runs',json=GOAL).json()['id'])
        assert final['status']=='completed'
        assert final['model_calls']==4
        assert 'invented-source' not in json.dumps(final['report'])
        events=c.get('/api/agent/runs/'+final['id']+'/events').json()
        assert any(x['kind']=='tool_finished' and not x['data']['ok'] for x in events)


def test_unknown_or_unauthorized_tool_not_executed(tmp_path):
    seen=[]
    def network(request):
        assert request.url.host=='api.openai.com'
        seen.append(1)
        name='search_library' if len(seen)==1 else 'run_shell'
        return httpx.Response(200,json=completion(name,{'query':'private','command':'cat /etc/passwd'}))
    with TestClient(create_app(str(tmp_path/'bad.sqlite3'),httpx.MockTransport(network)),headers=HEADERS) as c:
        c.put('/api/agent/config',json=CONFIG)
        final=wait_done(c,c.post('/api/agent/runs',json={**GOAL,'max_model_calls':2,'max_tool_calls':2}).json()['id'])
        assert final['status']=='budget_exhausted'
        assert final['evidence']==[] and final['report'] is None
        assert len(seen)==2


def test_cancel_stops_before_tool_and_configuration_is_locked(tmp_path):
    started,release=threading.Event(),threading.Event()
    requests=[]
    def network(request):
        requests.append(request.url.host)
        started.set()
        assert release.wait(5)
        return httpx.Response(200,json=completion('search_papers',{'query':'layout'}))
    with TestClient(create_app(str(tmp_path/'cancel.sqlite3'),httpx.MockTransport(network)),headers=HEADERS) as c:
        c.put('/api/agent/config',json=CONFIG)
        rid=c.post('/api/agent/runs',json=GOAL).json()['id']
        assert started.wait(2)
        assert c.put('/api/agent/config',json=CONFIG).status_code==409
        assert c.post('/api/agent/runs',json=GOAL).status_code==409
        assert c.post(f'/api/agent/runs/{rid}/cancel',json={}).status_code==200
        release.set()
        final=wait_done(c,rid)
        assert final['status']=='cancelled' and final['tool_calls']==0
        assert requests==['api.openai.com']
        assert c.post(f'/api/agent/runs/{rid}/resume',json={'confirmed':True}).status_code==409


def test_resume_uses_persisted_state_without_replaying_completed_tools(tmp_path):
    path=str(tmp_path/'resume.sqlite3')
    network=FixtureNetwork()
    original=network.__call__
    def fail_after_search(request):
        if request.url.host=='api.openai.com' and network.model_count>=2:
            return httpx.Response(503,json={'error':'fixture interruption'})
        return original(request)
    with TestClient(create_app(path,httpx.MockTransport(fail_after_search)),headers=HEADERS) as c:
        c.put('/api/agent/config',json=CONFIG)
        rid=c.post('/api/agent/runs',json=GOAL).json()['id']
        final=wait_done(c,rid)
        assert final['status']=='failed' and final['model_calls']==3 and len(final['evidence'])==1
    calls=[]
    def resume_network(request):
        calls.append(request.url.host)
        assert request.url.host=='api.openai.com'
        data=json.loads(request.content);check_protocol(data['messages'])
        ids=[x['id'] for m in data['messages'] if m['role']=='tool' for x in json.loads(m['content']).get('evidence',[])]
        return httpx.Response(200,json=completion('finish_report',report(ids)))
    with TestClient(create_app(path,httpx.MockTransport(resume_network)),headers=HEADERS) as c:
        assert not c.get('/api/agent/config').json()['configured']
        c.put('/api/agent/config',json={**CONFIG,'model':'different-model'})
        assert c.post(f'/api/agent/runs/{rid}/resume',json={'confirmed':True}).status_code==409
        c.put('/api/agent/config',json=CONFIG)
        assert c.post(f'/api/agent/runs/{rid}/resume',json={'confirmed':True}).status_code==202
        final=wait_done(c,rid)
        assert final['status']=='completed' and final['model_calls']==4
        assert final['resumes']==1 and len(final['evidence'])==1
        assert calls==['api.openai.com']


def test_search_crossref(client):
    network=httpx.MockTransport(lambda req:httpx.Response(200,json={'message':{'items':[{'DOI':'10.1234/test','title':['Fixture Paper'],'issued':{'date-parts':[[2025]]},'author':[{'given':'A','family':'B'}]}]}}))
    result=ResearchTools(client.app.state.store,network).search_papers(PaperSearchArgs(query='fixture',source='crossref'))
    assert result['documents'][0]['paper']['doi']=='10.1234/test'


def test_search_hub(client):
    seen=[]
    def network(req):
        seen.append(str(req.url));return httpx.Response(200,json=[{'id':'fixture/model'},{'id':'../escape'}])
    result=ResearchTools(client.app.state.store,httpx.MockTransport(network)).search_hub(HubSearchArgs(query='fixture'))
    assert len(result['documents'])==1
    assert result['documents'][0]['source_url']=='https://huggingface.co/fixture/model'


def test_read_repository_file_pinned_and_bounded(client):
    sha='a'*40
    def network(req):
        if '/commits/' in req.url.path:return httpx.Response(200,json={'sha':sha})
        assert req.url.params['ref']==sha
        return httpx.Response(200,json={'type':'file','encoding':'base64','size':100,'content':base64.b64encode(b'first\nsecond\nthird\n').decode()})
    tools=ResearchTools(client.app.state.store,httpx.MockTransport(network))
    result=tools.read_repository_file(FileArgs(repository='fixture/repo',path='README.md',start_line=2,line_count=1))
    assert result['documents'][0]['content']=='2: second'
    assert sha in result['documents'][0]['source_url']
    with pytest.raises(ValueError):tools.read_repository_file(FileArgs(repository='fixture/repo',path='../private.md'))
    with pytest.raises(ValueError):tools.read_repository_file(FileArgs(repository='fixture/repo',path='weights.bin'))


def test_library_privacy_and_old_data_preserved(client):
    store=client.app.state.store
    paper=store.create_paper(PaperInput(title='Layout private fixture',notes='DO NOT SEND ME',abstract='Public abstract'), owner=LOCAL_OWNER)
    tools=ResearchTools(store)
    with pytest.raises(ValueError):tools.execute('search_library',{'query':'layout'},use_library=False)
    # An unowned library search is refused too: there is no correct answer to "whose library", and
    # everybody's is the one answer that must never come back.
    with pytest.raises(ValueError):tools.execute('search_library',{'query':'layout'},use_library=True)
    result=tools.execute('search_library',{'query':'layout'},use_library=True,owner=LOCAL_OWNER)
    assert 'DO NOT SEND ME' not in json.dumps(result)
    assert store.get_paper(paper['id'], owner=LOCAL_OWNER)['notes']=='DO NOT SEND ME'
    assert all('search_library'!=x['function']['name'] for x in specifications())


def test_import_existing_paper_never_overwrites_notes(tmp_path):
    network=FixtureNetwork()
    with TestClient(create_app(str(tmp_path/'existing.sqlite3'),httpx.MockTransport(network)),headers=HEADERS) as c:
        old=c.post('/api/papers',json={'title':'My actual title','arxiv_id':'2501.12345v1','notes':'Keep my notes'}).json()
        c.put('/api/agent/config',json=CONFIG)
        final=wait_done(c,c.post('/api/agent/runs',json=GOAL).json()['id'])
        response=c.post(f"/api/agent/runs/{final['id']}/evidence/{final['evidence'][0]['id']}/import",json={'confirmed':True}).json()
        assert response['paper_id']==old['id'] and not response['created']
        saved=c.get('/api/papers/'+old['id']).json()
        assert saved['title']=='My actual title' and saved['notes']=='Keep my notes'


def test_omitted_task_budgets_fall_back_to_workspace_defaults():
    defaults=TaskDefaults(max_model_calls=6,max_tool_calls=9,max_upstream_requests=42,attempt_seconds=120,use_library=True,research_scope='expanded')
    inherited=defaults.merged(TaskInput(goal='检索 layout 论文与资源',consent_to_send=True))
    assert inherited.model_dump()=={'max_model_calls':6,'max_tool_calls':9,'max_upstream_requests':42,'attempt_seconds':120,'use_library':True,'research_scope':'expanded'}
    # An explicit per-task value still wins, including an explicit False.
    narrowed=defaults.merged(TaskInput(goal='检索 layout 论文与资源',consent_to_send=True,max_tool_calls=3,use_library=False))
    assert narrowed.max_tool_calls==3 and narrowed.use_library is False and narrowed.max_model_calls==6


def test_saved_defaults_persist_and_are_applied_to_new_tasks(tmp_path):
    network=FixtureNetwork();path=str(tmp_path/'defaults.sqlite3')
    with TestClient(create_app(path,httpx.MockTransport(network)),headers=HEADERS) as c:
        assert c.get('/api/agent/config').json()['task_defaults']=={'max_model_calls':12,'max_tool_calls':20,'max_upstream_requests':60,'attempt_seconds':360,'use_library':False,'research_scope':'focused'}
        saved=c.put('/api/agent/defaults',json={'max_model_calls':5,'max_tool_calls':4,'max_upstream_requests':17,'attempt_seconds':120,'use_library':True,'research_scope':'expanded'})
        assert saved.status_code==200
        assert saved.json()['task_defaults']['attempt_seconds']==120
        c.put('/api/agent/config',json=CONFIG)
        run=c.post('/api/agent/runs',json=GOAL).json()
        assert (run['params']['max_model_calls'],run['params']['max_tool_calls'],run['params']['max_upstream_requests'],run['params']['attempt_seconds'],run['params']['research_scope'])==(5,4,17,120,'expanded')
        assert run['params']['use_library'] is True
        assert wait_done(c,run['id'])['status']=='completed'
    # A later process on the same database must keep the saved defaults.
    with TestClient(create_app(path,httpx.MockTransport(network)),headers=HEADERS) as c:
        assert c.get('/api/agent/config').json()['task_defaults']['max_tool_calls']==4


def test_defaults_reject_invalid_values_without_changing_the_saved_row(client):
    for payload in ({'max_model_calls':99},{'max_tool_calls':0},{'max_upstream_requests':301},{'attempt_seconds':10},{'research_scope':'unknown'},{'max_model_calls':6,'unknown':1}):
        assert client.put('/api/agent/defaults',json=payload).status_code==422
    assert client.get('/api/agent/config').json()['task_defaults']['max_model_calls']==12


def test_clearing_model_memory_keeps_defaults_and_never_echoes_the_key(client):
    client.put('/api/agent/config',json=CONFIG)
    response=client.put('/api/agent/defaults',json={'max_model_calls':8,'use_library':True})
    assert response.status_code==200 and CONFIG['api_key'] not in response.text
    client.request('DELETE','/api/agent/config',json={})
    remaining=client.get('/api/agent/config').json()
    assert remaining['configured'] is False
    # Clearing the in-memory model must not silently reset workspace policy.
    assert remaining['task_defaults']=={'max_model_calls':8,'max_tool_calls':20,'max_upstream_requests':60,'attempt_seconds':360,'use_library':True,'research_scope':'focused'}
    assert CONFIG['api_key'] not in client.get('/api/agent/config').text


def test_endpoint_presets_stay_inside_the_destination_allowlist(client):
    presets=client.get('/api/agent/config').json()['endpoint_presets']
    assert presets and len({p['id'] for p in presets})==len(presets)
    for preset in presets:
        assert set(preset)=={'id','label','base_url'},preset
        host=urlsplit(preset['base_url']).hostname
        assert host in DEFAULT_HOSTS or host in LOOPBACK,preset


def test_model_list_requires_a_trusted_allowlisted_endpoint_and_a_key(client):
    assert client.post('/api/agent/models',json={'base_url':CONFIG['base_url']}).status_code==422
    assert client.post('/api/agent/models',json={'base_url':'https://evil.example/v1','api_key':'sk-aaaaaaaa','trust_endpoint':True}).status_code==422
    assert client.post('/api/agent/models',json={'base_url':CONFIG['base_url'],'trust_endpoint':True}).status_code==422
    assert client.post('/api/agent/models',json={'base_url':CONFIG['base_url'],'api_key':'bad\nkey','trust_endpoint':True}).status_code==422
    assert client.post('/api/agent/models',json={'base_url':CONFIG['base_url'],'api_key':'sk-aaaaaaaa','trust_endpoint':False}).status_code==422


def test_model_list_returns_sorted_unique_ids_without_storing_or_echoing_the_key(tmp_path):
    network=FixtureNetwork()
    with TestClient(create_app(str(tmp_path/'models.sqlite3'),httpx.MockTransport(network)),headers=HEADERS) as c:
        response=c.post('/api/agent/models',json={'base_url':CONFIG['base_url'],'api_key':CONFIG['api_key'],'trust_endpoint':True})
        assert response.status_code==200,response.text
        body=response.json()
        assert body['models']==['fixture-model','fixture-model-pro'] and body['total']==2 and not body['truncated']
        assert '工具调用' in body['note']
        assert CONFIG['api_key'] not in response.text
        assert str(network.requests[-1].url)=='https://api.openai.com/v1/models'
        assert network.requests[-1].headers['Authorization']=='Bearer '+CONFIG['api_key']
        # Probing is read-only: it must not leave a model configuration behind.
        assert not c.get('/api/agent/config').json()['configured']


def test_parallel_model_list_clicks_for_one_owner_send_only_one_probe(tmp_path):
    entered, release = threading.Event(), threading.Event()
    requests = []
    def network(request):
        requests.append(request)
        entered.set()
        assert release.wait(5)
        return httpx.Response(200, json={'data': [{'id': 'fixture-model'}]})
    with TestClient(create_app(str(tmp_path/'parallel-model-probes.sqlite3'),
                               httpx.MockTransport(network)),headers=HEADERS) as c:
        payload={'base_url':CONFIG['base_url'],'api_key':CONFIG['api_key'],'trust_endpoint':True}
        with ThreadPoolExecutor(max_workers=1) as pool:
            first=pool.submit(c.post,'/api/agent/models',json=payload)
            assert entered.wait(2)
            refused=[c.post('/api/agent/models',json=payload) for _ in range(20)]
            assert all(response.status_code==409 for response in refused)
            assert len(requests)==1, 'the refused parallel probes must not open another provider request'
            release.set()
            response=first.result(timeout=5)
        assert response.status_code==200,response.text
        assert len(requests)==1
        probes=c.get('/api/agent/config').json()['quota']['probes']
        assert probes['counts']['models']==1 and probes['active'] is False


def test_model_list_failure_never_relays_the_provider_body_or_the_key(tmp_path):
    def refuse(request):
        return httpx.Response(401,json={'error':{'message':'invalid key '+CONFIG['api_key']}})
    with TestClient(create_app(str(tmp_path/'refused.sqlite3'),httpx.MockTransport(refuse)),headers=HEADERS) as c:
        response=c.post('/api/agent/models',json={'base_url':CONFIG['base_url'],'api_key':CONFIG['api_key'],'trust_endpoint':True})
        assert response.status_code==422
        assert CONFIG['api_key'] not in response.text and 'invalid key' not in response.text
        assert '401' in response.json()['detail']


def test_model_list_rejects_an_oversized_or_non_json_response(tmp_path):
    def oversized(request):
        return httpx.Response(200,content=b'{"data":[' + b'"model-x",' * 64000 + b']}')
    with TestClient(create_app(str(tmp_path/'oversized.sqlite3'),httpx.MockTransport(oversized)),headers=HEADERS) as c:
        assert c.post('/api/agent/models',json={'base_url':CONFIG['base_url'],'api_key':CONFIG['api_key'],'trust_endpoint':True}).status_code==422
    def not_json(request):
        return httpx.Response(200,content=b'<html>no</html>')
    with TestClient(create_app(str(tmp_path/'notjson.sqlite3'),httpx.MockTransport(not_json)),headers=HEADERS) as c:
        response=c.post('/api/agent/models',json={'base_url':CONFIG['base_url'],'api_key':CONFIG['api_key'],'trust_endpoint':True})
        assert response.status_code==422 and 'JSON' in response.json()['detail']


def test_model_vault_expiry_and_generation_invalidate_old_snapshots():
    now = [100.0]
    vault = ModelVault(clock=lambda: now[0])
    vault.set(ModelConfig(**CONFIG), owner="alice")
    snapshot, generation = vault.snapshot_with_generation(owner="alice")
    assert snapshot.api_key.get_secret_value() == CONFIG["api_key"]
    assert vault.is_current("alice", generation)

    now[0] += MODEL_KEY_TTL.total_seconds() + 0.01
    assert not vault.is_current("alice", generation)
    assert vault.public(owner="alice")["configured"] is False
    with pytest.raises(ModelError, match="尚未配置模型"):
        vault.snapshot(owner="alice")

    vault.set(ModelConfig(**CONFIG), owner="alice")
    _, replacement_generation = vault.snapshot_with_generation(owner="alice")
    vault.clear(owner="alice")
    assert not vault.is_current("alice", replacement_generation)


def test_hosted_chat_call_revalidates_destination_immediately_before_network(monkeypatch):
    from re0.agent import model as model_module

    requests = []

    def contact(request):
        requests.append(request)
        return httpx.Response(200, json=completion("connection_check", {}))

    transport = httpx.MockTransport(contact)
    monkeypatch.setattr(model_module, "_require_public_resolution", lambda hostname: None)
    model = ChatModel(ModelConfig(**CONFIG), transport, hosted=True)
    monkeypatch.setattr(model_module, "_require_public_resolution",
                        lambda hostname: (_ for _ in ()).throw(ModelError("private resolution")))
    with pytest.raises(ModelError, match="private resolution"):
        model.test()
    assert requests == []


def test_tool_result_keeps_metadata_with_a_bounded_excerpt(client):
    agent=client.app.state.agent
    view=agent.model_view({'ok':True,'evidence':[{'id':'ev_1','kind':'code','locator':'train.py:1-200','source_url':'https://github.com/lab/paper','content':'x'*20000}]})
    item=view['evidence'][0]
    assert item['id']=='ev_1' and item['locator']=='train.py:1-200' and item['source_url']=='https://github.com/lab/paper'
    assert item['content_chars']==20000 and 'content' not in item
    assert len(item['excerpt'])==6000 and item['elided'] is True
    assert 'read_evidence' in view['evidence_note']
    assert len(json.dumps(view,ensure_ascii=False))<20000


def test_the_excerpt_budget_is_shared_across_one_tool_result(client):
    agent=client.app.state.agent
    view=agent.model_view({'ok':True,'evidence':[{'id':f'ev_{i}','kind':'source','content':'y'*4000} for i in range(6)]})
    assert [len(x['excerpt']) for x in view['evidence']]==[4000,2000,0,0,0,0]
    assert view['evidence'][0]['elided'] is False and view['evidence'][1]['elided'] is True
    assert all(x['content_chars']==4000 for x in view['evidence'])


def test_read_evidence_slices_a_stored_body_and_rejects_bad_requests(tmp_path):
    def multi(calls):
        return {"choices":[{"finish_reason":"tool_calls","message":{"role":"assistant","content":"","tool_calls":[
            {"id":f"call_read_{index}","type":"function","function":{"name":name,"arguments":json.dumps(args,ensure_ascii=False)}}
            for index,(name,args) in enumerate(calls)]}}],"usage":{"prompt_tokens":10,"completion_tokens":5,"total_tokens":15}}
    class ReadBack:
        def __init__(self):
            self.prompts=[]
        def __call__(self,request):
            if request.url.host=='export.arxiv.org':
                return httpx.Response(200,content=ATOM)
            data=json.loads(request.content)
            if isinstance(data.get('tool_choice'),dict):
                return httpx.Response(200,json=completion('connection_check',{}))
            self.prompts.append(data)
            step=len(self.prompts)
            if step==1:
                return httpx.Response(200,json=completion('update_plan',{'steps':['检索','取回正文','整理报告']}))
            if step==2:
                return httpx.Response(200,json=completion('search_papers',{'query':'layout','limit':1,'source':'arxiv'}))
            known=[ev['id'] for m in data['messages'] if m['role']=='tool' for ev in json.loads(m['content']).get('evidence',[])]
            if step==3:
                # One round: a valid slice, a foreign id, and a read below the schema floor.
                return httpx.Response(200,json=multi([('read_evidence',{'evidence_id':known[0],'offset':0,'chars':200}),
                                                      ('read_evidence',{'evidence_id':'ev_not_mine','offset':0,'chars':200}),
                                                      ('read_evidence',{'evidence_id':known[0],'offset':0,'chars':10})]))
            return httpx.Response(200,json=completion('finish_report',report(known)))
    network=ReadBack()
    with TestClient(create_app(str(tmp_path/'readback.sqlite3'),httpx.MockTransport(network)),headers=HEADERS) as c:
        c.put('/api/agent/config',json=CONFIG)
        final=wait_done(c,c.post('/api/agent/runs',json=GOAL).json()['id'])
        assert final['status']=='completed',final
        payloads=[json.loads(m['content']) for m in network.prompts[-1]['messages'] if m['role']=='tool']
        # The conversation carries metadata and a bounded excerpt, not whole bodies.
        search=next(p for p in payloads if p.get('evidence'))
        item=search['evidence'][0]
        assert 'content' not in item and item['content_chars']>0
        assert 'excerpt' in item and 'elided' in item and 'read_evidence' in search['evidence_note']
        # A valid read serves an exact slice of the body kept in the evidence store.
        served=next(p for p in payloads if p.get('evidence_id'))
        body=next(e['content'] for e in final['evidence'] if e['id']==served['evidence_id'])
        assert served['content']==body[:200] and len(served['content'])==min(200,len(body))
        assert served['total_chars']==len(body) and served['truncated'] is (len(body)>200)
        assert served['locator'] and served['source_url'] and 'offset' in served
        # A foreign id and a below-floor read are both refused, never silently widened.
        assert len([p for p in payloads if p.get('ok') is False])==2,payloads


def test_context_compaction_elides_older_excerpts_without_losing_evidence(tmp_path,monkeypatch):
    monkeypatch.setattr('re0.agent.runtime.CONTEXT_COMPACT_CHARS',300)
    monkeypatch.setattr('re0.agent.runtime.KEEP_RECENT_TOOL_MESSAGES',0)
    network=FixtureNetwork()
    with TestClient(create_app(str(tmp_path/'compact.sqlite3'),httpx.MockTransport(network)),headers=HEADERS) as c:
        c.put('/api/agent/config',json=CONFIG)
        rid=c.post('/api/agent/runs',json=GOAL).json()['id']
        final=wait_done(c,rid)
        assert final['status']=='completed',final
        # Compaction must never remove the evidence itself.
        assert final['evidence'],'evidence did not survive compaction'
        compacted=[e for e in c.get(f'/api/agent/runs/{rid}/events').json() if e['kind']=='context_compacted']
        assert compacted and 'read_evidence' in compacted[0]['data']['message'],compacted
        # The model must be told, in-band, how to fetch an elided body back.
        exchanges=[json.loads(r.content)['messages'] for r in network.requests if r.url.path=='/v1/chat/completions']
        assert any('read_evidence' in m['content'] for p in exchanges for m in p if m['role']=='user')
