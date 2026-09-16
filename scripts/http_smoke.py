"""Real loopback HTTP smoke. No browser interception, model or external API calls."""
from __future__ import annotations

import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time

import httpx

ROOT = Path(__file__).resolve().parents[1]


def run(output: Path):
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0))
            port = sock.getsockname()[1]
        env = {k: v for k, v in os.environ.items() if not k.startswith('RE0_') and k not in {'TAVILY_API_KEY', 'GITHUB_TOKEN'}}
        env.update(RE0_DB=str(Path(tmp) / 'smoke.sqlite3'), RE0_HOST='127.0.0.1', RE0_PORT=str(port))
        with (Path(tmp) / 'server.log').open('w') as log:
            process = subprocess.Popen([sys.executable, 'run.py'], cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT)
            try:
                with httpx.Client(base_url=f'http://127.0.0.1:{port}', timeout=3, trust_env=False) as client:
                    deadline = time.monotonic() + 12
                    while True:
                        try:
                            response = client.get('/api/health')
                            if response.status_code == 200:
                                break
                        except httpx.HTTPError:
                            pass
                        if time.monotonic() >= deadline or process.poll() is not None:
                            raise RuntimeError('Local server did not become ready; inspect installed dependencies')
                        time.sleep(0.1)
                    assert response.json()['version'] == '0.2.0' and not response.json()['llm_enabled']
                    paths = ['/api/health', '/', '/library', '/static/agent.js', '/static/agent.css', '/api/agent/config', '/openapi.json']
                    results = []
                    for path in paths:
                        res = client.get(path)
                        assert res.status_code == 200, (path, res.status_code)
                        results.append({'path': path, 'status': res.status_code})
                    payload = {'goal': 'HTTP smoke; no model or external query', 'consent_to_send': True}
                    assert client.post('/api/agent/runs', json=payload).status_code == 403
                    headers = {'X-Re0-Client': 'web'}
                    res = client.post('/api/agent/runs', json=payload, headers=headers)
                    assert res.status_code == 422 and '尚未配置模型' in res.text
                    res = client.post('/api/agent/runs', json=payload, headers={**headers, 'Origin': 'https://untrusted.example'})
                    assert res.status_code == 403
                    assert client.get('/api/agent/runs').json() == []
            finally:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=3)
    result = {'mode': 'actual loopback HTTP process; no browser, external APIs or model', 'version': '0.2.0',
              'paths': results, 'guards': ['missing-client-header:403', 'no-model:422', 'cross-origin:403'],
              'tasks_created': 0}
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2))
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    run(Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / '.data/http-smoke-agent.json')
