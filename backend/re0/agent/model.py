"""BYOK gateway. Credentials never enter prompts, checkpoint JSON or responses."""
from __future__ import annotations

import json
import os
import threading
import time
from urllib.parse import urlsplit

import httpx
from pydantic import ValidationError

from .schemas import ModelConfig

DEFAULT_HOSTS = {"api.openai.com", "api.deepseek.com", "dashscope.aliyuncs.com",
                 "dashscope-intl.aliyuncs.com", "openrouter.ai"}
LOOPBACK = {"127.0.0.1", "::1"}


class ModelError(Exception):
    """Only application-authored, credential-free messages may be exposed."""


def validate_endpoint(config: ModelConfig) -> ModelConfig:
    try:
        p = urlsplit(config.base_url)
        allowed = DEFAULT_HOSTS | {h.strip().lower() for h in os.getenv("RE0_LLM_ALLOWED_HOSTS", "").split(",") if h.strip()}
        if p.username or p.password or p.query or p.fragment or not p.hostname:
            raise ValueError
        if p.hostname in LOOPBACK:
            if p.scheme not in {"http", "https"} or not p.port:
                raise ValueError
        elif p.scheme != "https" or p.hostname not in allowed or p.port not in (None, 443):
            raise ValueError
        if any(s in {".", ".."} for s in p.path.split("/")) or "%" in p.path or "\\" in p.path:
            raise ValueError
        if p.path.endswith("/chat/completions"):
            raise ModelError("请填写 Base URL（通常到 /v1），不要包含 /chat/completions")
        if p.hostname not in LOOPBACK and not config.api_key.get_secret_value():
            raise ModelError("远程模型服务需要 API Key")
    except ValueError as exc:
        raise ModelError("模型地址不受信任：使用预设 HTTPS 主机，或显式端口的 127.0.0.1 / [::1]；自定义主机需由部署者设置 RE0_LLM_ALLOWED_HOSTS") from exc
    return config.model_copy(update={"base_url": config.base_url.rstrip("/")})


class ModelVault:
    def __init__(self):
        self._lock = threading.RLock()
        self._config = None
        self.startup_error = ""
        if os.getenv("RE0_LLM_MODEL") and os.getenv("RE0_LLM_BASE_URL"):
            try:
                self.set(ModelConfig(base_url=os.environ["RE0_LLM_BASE_URL"], model=os.environ["RE0_LLM_MODEL"],
                                     api_key=os.getenv("RE0_LLM_API_KEY", ""), trust_endpoint=True,
                                     token_parameter=os.getenv("RE0_LLM_TOKEN_PARAMETER", "max_tokens")))
            except (ModelError, ValidationError):
                self.startup_error = "环境中的模型配置无效；请在模型设置中重新配置"

    def set(self, config):
        with self._lock:
            self._config = validate_endpoint(config)

    def clear(self):
        with self._lock:
            self._config = None

    def snapshot(self):
        with self._lock:
            if self._config is None:
                raise ModelError("尚未配置模型：请先设置 Base URL、Model ID 和 API Key")
            return self._config.model_copy(deep=True)

    def public(self):
        with self._lock:
            return self._config.public() if self._config else {"configured": False, "storage": "server_memory", "startup_error": self.startup_error}


def redact(text: str, config: ModelConfig) -> str:
    key = config.api_key.get_secret_value()
    return text.replace(key, "[REDACTED]") if key else text


class ChatModel:
    def __init__(self, config: ModelConfig, transport=None):
        self.config, self.transport = validate_endpoint(config), transport

    def complete(self, messages: list[dict], tools: list[dict], *, timeout=60, force_tool=None) -> dict:
        payload = {"model": self.config.model, "messages": messages, "tools": tools,
                   self.config.token_parameter: self.config.max_output_tokens,
                   "tool_choice": {"type": "function", "function": {"name": force_tool}} if force_tool else "auto",
                   "stream": False}
        if len(json.dumps(payload, ensure_ascii=False)) > 150000:
            raise ModelError("任务上下文达到 150,000 字符上限；请缩小研究范围并新建任务")
        headers = {"Content-Type": "application/json"}
        key = self.config.api_key.get_secret_value()
        if key:
            headers["Authorization"] = f"Bearer {key}"
        end = time.monotonic() + timeout
        try:
            with httpx.Client(transport=self.transport, timeout=timeout, follow_redirects=False, trust_env=False) as client:
                with client.stream("POST", self.config.base_url + "/chat/completions", json=payload, headers=headers) as response:
                    if response.status_code != 200:
                        # Do not leak provider error bodies, request headers, URLs or keys.
                        raise ModelError(f"模型接口返回 HTTP {response.status_code}；请检查权限、余额、模型名、工具调用支持和 token 参数")
                    body = bytearray()
                    for chunk in response.iter_bytes():
                        body.extend(chunk)
                        if len(body) > 2 * 1024 * 1024 or time.monotonic() > end:
                            raise ModelError("模型响应超过大小或时间上限；本次结果未采纳")
            obj = json.loads(body)
            choice = obj["choices"][0]
            msg = choice["message"]
            if choice.get("finish_reason") in {"length", "content_filter"}:
                raise ModelError("模型输出被截断或拒绝；未将不完整响应写为结论")
            calls = msg.get("tool_calls") or []
            if not isinstance(calls, list) or len(calls) > 4:
                raise ModelError("每次模型响应最多允许 4 个工具调用")
            cleaned, ids = [], set()
            for call in calls:
                cid, fn = call["id"], call["function"]
                if (not isinstance(cid, str) or not cid or len(cid) > 200 or cid in ids
                        or not isinstance(fn["name"], str) or len(fn["name"]) > 80
                        or not isinstance(fn["arguments"], str) or len(fn["arguments"]) > 16000):
                    raise ModelError("模型返回了无效工具调用")
                ids.add(cid)
                cleaned.append({"id": cid, "type": "function", "function": {
                    "name": fn["name"], "arguments": redact(fn["arguments"], self.config)}})
            text = msg.get("content") or ""
            if not isinstance(text, str) or len(text) > 32000:
                raise ModelError("模型文本格式无效或过长")
            assistant = {"role": "assistant", "content": redact(text, self.config)}
            if cleaned:
                assistant["tool_calls"] = cleaned
            usage = obj.get("usage") or {}
            counts = {name: min(10**8, max(0, int(usage.get(name, 0)))) for name in ("prompt_tokens", "completion_tokens", "total_tokens")}
            return {"message": assistant, "usage": counts, "usage_reported": bool(usage)}
        except httpx.HTTPError as exc:
            raise ModelError("模型连接或读取失败；检查网络和模型服务地址。没有自动切换服务，也没有自动重试付费请求") from exc
        except (KeyError, IndexError, TypeError, ValueError, AttributeError) as exc:
            raise ModelError("模型响应不符合 Chat Completions tool_calls 协议") from exc

    def test(self):
        tool = {"type": "function", "function": {"name": "connection_check", "description": "Connectivity check, no external action.",
                "parameters": {"type": "object", "properties": {}, "required": [], "additionalProperties": False}}}
        result = self.complete([{"role": "user", "content": "Call connection_check with an empty object."}], [tool], timeout=30, force_tool="connection_check")
        calls = result["message"].get("tool_calls", [])
        ok = len(calls) == 1 and calls[0]["function"]["name"] == "connection_check"
        try:
            ok = ok and json.loads(calls[0]["function"]["arguments"]) == {}
        except (ValueError, IndexError):
            ok = False
        if not ok:
            raise ModelError("接口可连接，但没有返回有效工具调用；此模型目前不能用于本 agent")
        return {"ok": True, "tool_calling": True, "usage": result["usage"], "note": "只验证了本次工具调用协议，不代表科研效果已评测"}
