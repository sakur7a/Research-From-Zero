"""Test-only loopback ASGI server for the real-browser Agent smoke.

The browser and application use ordinary TCP/HTTP. Model and research-provider calls are
intercepted inside this process by the existing deterministic MatrixFixture.
"""
from __future__ import annotations

import json
import socket
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
sys.path.insert(0, str(ROOT / "backend" / "tests"))

import httpx
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, Response
import uvicorn

from re0.main import create_app
from test_agent_matrix import MatrixFixture, collect_evidence, tool_completion


class FollowupMatrixFixture(MatrixFixture):
    """Reuse the matrix fixture for one second turn without any live provider traffic."""

    def __call__(self, request):
        if (request.url.host != "api.openai.com"
                or request.url.path != "/v1/chat/completions"):
            return super().__call__(request)
        data = json.loads(request.content)
        if isinstance(data.get("tool_choice"), dict) or self.model_calls < 7:
            if (self.model_calls == 0 and not isinstance(data.get("tool_choice"), dict)):
                # Leave a small window for the second hosted guest to attempt a cross-owner cancel.
                time.sleep(1.2)
            return super().__call__(request)

        self.requests.append(request)
        self.model_calls += 1
        self.sequence += 1
        call_id = f"http-followup-{self.sequence}"
        if self.model_calls == 8:
            return httpx.Response(200, json=tool_completion(
                call_id, "update_plan", {"steps": ["按新增条件复查候选", "提交追问结果"]}))
        if self.model_calls == 9:
            return httpx.Response(200, json=tool_completion(
                call_id, "search_papers", {"query": "LayerKit training follow-up",
                                             "source": "arxiv", "limit": 3}))
        if self.model_calls == 10:
            papers = [item for item in collect_evidence(data["messages"])
                      if item.get("kind") == "paper" and item.get("paper")]
            if not papers:
                raise AssertionError("the follow-up fixture found no current paper evidence")
            report = {
                "title": "Fixture follow-up report",
                "summary": "The second turn used a new constrained paper query; this is a UI fixture.",
                "findings": [{"claim": "The follow-up returned a paper metadata record.",
                              "evidence_ids": [papers[-1]["id"]], "assessment": "observed"}],
                "limitations": ["Fixture-only follow-up; no live research quality is evaluated."],
                "outcome": "findings",
            }
            return httpx.Response(200, json=tool_completion(call_id, "finish_report", report))
        raise AssertionError(f"real-HTTP smoke exceeded its two-turn fixture: {self.model_calls}")


def use_public_fixture_dns():
    original = socket.getaddrinfo

    def resolve(host, *args, **kwargs):
        if str(host).lower() == "api.openai.com":
            return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "",
                     ("93.184.216.34", 443))]
        return original(host, *args, **kwargs)

    socket.getaddrinfo = resolve


def main():
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--database", required=True)
    parser.add_argument("--certificate")
    parser.add_argument("--private-key")
    args = parser.parse_args()
    if bool(args.certificate) != bool(args.private_key):
        parser.error("--certificate and --private-key must be provided together")

    use_public_fixture_dns()
    fixture = FollowupMatrixFixture()
    app = create_app(args.database, transport=httpx.MockTransport(fixture))

    # This route exists only in this test process; production has no smoke or fixture endpoint.
    @app.get("/__smoke__/metrics")
    def metrics():
        counts = {}
        for request in fixture.requests:
            counts[request.url.host] = counts.get(request.url.host, 0) + 1
        return {"mock_requests": len(fixture.requests), "by_host": counts,
                "agent_model_calls": fixture.model_calls}

    @app.get("/__smoke__/session-probe")
    def session_probe_page():
        return HTMLResponse("""<!doctype html><html><head><meta charset="utf-8"><title>Session probe</title></head>
          <body><pre id="result" data-done="false">checking</pre>
          <script src="/__smoke__/session-probe.js"></script></body></html>""")

    @app.get("/__smoke__/session-probe.js")
    def session_probe_script():
        script = r"""const runId = encodeURIComponent(new URLSearchParams(location.search).get('run') || '');
const request = async (name, path, method = 'GET') => {
  const options = {method, credentials: 'same-origin'};
  if (method !== 'GET') {
    options.headers = {'Content-Type': 'application/json', 'X-Re0-Client': 'web'};
    options.body = '{}';
  }
  const response = await fetch(path, options);
  if (name === 'config' && response.ok) return {name, status: response.status, configured: (await response.json()).configured};
  if (name === 'runs' && response.ok) return {name, status: response.status, count: (await response.json()).length};
  if (name === 'export') await response.arrayBuffer();
  else await response.text();
  return {name, status: response.status};
};
(async () => {
  const root = document.querySelector('#result');
  try {
    const results = await Promise.all([
      request('config', '/api/agent/config'),
      request('runs', '/api/agent/runs'),
      request('detail', `/api/agent/runs/${runId}`),
      request('export', `/api/agent/runs/${runId}/export`),
      request('cancel', `/api/agent/runs/${runId}/cancel`, 'POST'),
    ]);
    root.textContent = JSON.stringify(results);
  } catch (error) {
    root.textContent = `probe error: ${error.name}`;
  }
  root.dataset.done = 'true';
})();"""
        return Response(script, media_type="text/javascript")

    tls = ({"ssl_certfile": args.certificate, "ssl_keyfile": args.private_key}
           if args.certificate else {})
    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="error",
                access_log=False, **tls)


if __name__ == "__main__":
    main()
