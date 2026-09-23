"""CI-only app launcher that substitutes a deterministic fake model, never shipped in the image.

The Dockerfile, FastAPI app, SQLite stores, auth, task loop, routes and report export stay real. Only
the provider-facing model object is substituted so the container smoke spends no API money and makes
no external model request.
"""
from __future__ import annotations

import json
import os
import socket
import time

from re0.deployment import from_env
from re0.main import create_app
from re0.serve import check_deployment, server_kwargs


class SmokeModel:
    def __init__(self):
        self.calls = 0

    @staticmethod
    def _reply(name: str, args: dict) -> dict:
        return {
            "message": {
                "role": "assistant", "content": "",
                "tool_calls": [{"id": f"container-smoke-{name}", "type": "function",
                                "function": {"name": name,
                                             "arguments": json.dumps(args, ensure_ascii=False)}}],
            },
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
            "usage_reported": True,
        }

    def complete(self, messages, tools, *, timeout=60):
        self.calls += 1
        if self.calls == 1:
            # Leave time for the HTTP client to observe the accepted 202 task as active while polling.
            time.sleep(0.6)
            return self._reply("update_plan", {"steps": ["检索已授权的本地 fixture", "导出带证据的 smoke 报告"]})
        if self.calls == 2:
            if "search_library" not in {item["function"]["name"] for item in tools}:
                raise AssertionError("the authorized task did not receive search_library")
            return self._reply("search_library", {"query": "container smoke fixture", "limit": 1})
        if self.calls == 3:
            evidence_id = ""
            for message in reversed(messages):
                if message.get("role") != "tool":
                    continue
                result = json.loads(message.get("content") or "{}")
                evidence = result.get("evidence") or []
                if evidence:
                    evidence_id = evidence[0].get("id", "")
                    break
            if not evidence_id:
                raise AssertionError("the fixture library search returned no evidence")
            return self._reply("finish_report", {
                "title": "Container HTTP smoke",
                "summary": "The task searched the explicitly authorized CI fixture and exported its source evidence.",
                "findings": [{"claim": "The seeded fixture appeared in the authorized local-library search.",
                              "evidence_ids": [evidence_id], "assessment": "observed"}],
                "limitations": ["This is a deterministic container smoke, not a live model or research-quality evaluation."],
                "outcome": "findings",
            })
        raise AssertionError("the container smoke model exceeded its three-call script")


def use_public_fixture_dns():
    """Keep model endpoint validation offline while using a syntactically allowed public hostname."""
    original = socket.getaddrinfo

    def resolve(host, *args, **kwargs):
        if str(host).lower() == "api.openai.com":
            return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "",
                     ("93.184.216.34", 443))]
        return original(host, *args, **kwargs)

    socket.getaddrinfo = resolve


def main():
    use_public_fixture_dns()
    deployment = from_env().require_startable()
    app = create_app(deployment=deployment, model_factory=lambda _config: SmokeModel())
    kwargs = server_kwargs()
    check_deployment(kwargs["host"])
    import uvicorn

    uvicorn.run(app, **kwargs)


if __name__ == "__main__":
    main()
