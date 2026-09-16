"""Offline behavioral tests: real gateway/runtime/tools, fixture model and provider HTTP."""
import base64
import json
import threading
import time

import httpx
import pytest
from fastapi.testclient import TestClient

from re0.main import create_app
from re0.models import PaperInput
from re0.agent.model import ChatModel, ModelError, validate_endpoint
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
    deadline = time.monotonic() + 10
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
    paper=store.create_paper(PaperInput(title='Layout private fixture',notes='DO NOT SEND ME',abstract='Public abstract'))
    tools=ResearchTools(store)
    with pytest.raises(ValueError):tools.execute('search_library',{'query':'layout'},use_library=False)
    result=tools.execute('search_library',{'query':'layout'},use_library=True)
    assert 'DO NOT SEND ME' not in json.dumps(result)
    assert store.get_paper(paper['id'])['notes']=='DO NOT SEND ME'
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
    defaults=TaskDefaults(max_model_calls=6,max_tool_calls=9,attempt_seconds=120,use_library=True)
    inherited=defaults.merged(TaskInput(goal='检索 layout 论文与资源',consent_to_send=True))
    assert inherited.model_dump()=={'max_model_calls':6,'max_tool_calls':9,'attempt_seconds':120,'use_library':True}
    # An explicit per-task value still wins, including an explicit False.
    narrowed=defaults.merged(TaskInput(goal='检索 layout 论文与资源',consent_to_send=True,max_tool_calls=3,use_library=False))
    assert narrowed.max_tool_calls==3 and narrowed.use_library is False and narrowed.max_model_calls==6


def test_saved_defaults_persist_and_are_applied_to_new_tasks(tmp_path):
    network=FixtureNetwork();path=str(tmp_path/'defaults.sqlite3')
    with TestClient(create_app(path,httpx.MockTransport(network)),headers=HEADERS) as c:
        assert c.get('/api/agent/config').json()['task_defaults']=={'max_model_calls':12,'max_tool_calls':20,'attempt_seconds':360,'use_library':False}
        saved=c.put('/api/agent/defaults',json={'max_model_calls':5,'max_tool_calls':4,'attempt_seconds':120,'use_library':True})
        assert saved.status_code==200
        assert saved.json()['task_defaults']['attempt_seconds']==120
        c.put('/api/agent/config',json=CONFIG)
        run=c.post('/api/agent/runs',json=GOAL).json()
        assert (run['params']['max_model_calls'],run['params']['max_tool_calls'],run['params']['attempt_seconds'])==(5,4,120)
        assert run['params']['use_library'] is True
        assert wait_done(c,run['id'])['status']=='completed'
    # A later process on the same database must keep the saved defaults.
    with TestClient(create_app(path,httpx.MockTransport(network)),headers=HEADERS) as c:
        assert c.get('/api/agent/config').json()['task_defaults']['max_tool_calls']==4


def test_defaults_reject_invalid_values_without_changing_the_saved_row(client):
    for payload in ({'max_model_calls':99},{'max_tool_calls':0},{'attempt_seconds':10},{'max_model_calls':6,'unknown':1}):
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
    assert remaining['task_defaults']=={'max_model_calls':8,'max_tool_calls':20,'attempt_seconds':360,'use_library':True}
    assert CONFIG['api_key'] not in client.get('/api/agent/config').text
