"""BYOK gateway. Credentials never enter prompts, checkpoint JSON or responses.

One configuration per owner, held in server memory for the length of the process. A single
global configuration would mean that whoever configured last decides where everybody else's
prompts and evidence are sent — which is not a multi-user inconvenience but a leak with a bill
attached, since the other reader's task keeps running against the new destination.
"""
from __future__ import annotations

import json
import os
import threading
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit

import httpx
from pydantic import ValidationError

from ..deployment import LOCAL_OWNER
from .schemas import ModelConfig, ModelListRequest

# A destination permit, not a compatibility claim. Every host here still has to
# pass the tool-call capability check before it can run a research task.
DEFAULT_HOSTS = {"api.openai.com", "api.deepseek.com", "dashscope.aliyuncs.com",
                 "dashscope-intl.aliyuncs.com", "openrouter.ai",
                 "open.bigmodel.cn", "api.moonshot.cn", "api.siliconflow.cn",
                 "ark.cn-beijing.volces.com"}
LOOPBACK = {"127.0.0.1", "::1"}
MODEL_KEY_TTL = timedelta(hours=8)

# Offered to the settings UI so a user only has to paste a key. Hosts must stay
# inside DEFAULT_HOSTS or an existing deployment variable; see the drift test.
ENDPOINT_PRESETS = [
    {"id": "openai", "label": "OpenAI", "base_url": "https://api.openai.com/v1"},
    {"id": "deepseek", "label": "DeepSeek", "base_url": "https://api.deepseek.com/v1"},
    {"id": "dashscope", "label": "阿里云百炼 · 通义千问", "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1"},
    {"id": "dashscope-intl", "label": "阿里云百炼 · 国际站", "base_url": "https://dashscope-intl.aliyuncs.com/compatible-mode/v1"},
    {"id": "moonshot", "label": "月之暗面 Kimi", "base_url": "https://api.moonshot.cn/v1"},
    {"id": "zhipu", "label": "智谱 AI · GLM", "base_url": "https://open.bigmodel.cn/api/paas/v4"},
    {"id": "siliconflow", "label": "硅基流动", "base_url": "https://api.siliconflow.cn/v1"},
    {"id": "volcengine", "label": "火山方舟 · 豆包", "base_url": "https://ark.cn-beijing.volces.com/api/v3"},
    {"id": "openrouter", "label": "OpenRouter", "base_url": "https://openrouter.ai/api/v1"},
    {"id": "ollama", "label": "本地服务 · Ollama", "base_url": "http://127.0.0.1:11434/v1"},
]

MODEL_LIST_LIMIT = 400
MODEL_LIST_BYTES = 512 * 1024


class ModelError(Exception):
    """Only application-authored, credential-free messages may be exposed.

    `destination` marks the failures that say something about the *service* rather than about this
    one caller's credential: unreachable, timed out, or answering with a server error. It is the only
    kind the site-wide circuit breaker counts, because one account's wrong key or empty balance must
    not be able to stop everybody else's work.
    """

    def __init__(self, message: str, *, destination: bool = False):
        super().__init__(message)
        self.destination = destination


# 408 and 429 are "the service could not take this right now" and 5xx is the service's own failure, so
# a run of them describes an outage. 401/402/403/404 describe a credential, a balance or a model name
# this caller got wrong: retrying them will not help anybody, and stopping them is not the site's job.
DESTINATION_STATUSES = frozenset({408, 429})


def _is_destination_status(status: int) -> bool:
    return status in DESTINATION_STATUSES or status >= 500


def validate_endpoint(config: "ModelConfig | ModelListRequest", *, hosted: bool = False):
    """Refuse a destination this deployment must not talk to.

    A hostname allowlist is the first gate and it is not enough on its own: an allowlisted name can
    resolve to a private address, and in hosted mode that turns a model call into a probe of the
    network the service runs on. So hosted mode also refuses loopback outright — a local Ollama is
    a single-user convenience, not something a public service should reach for a stranger — and
    requires every address the name resolves to be public.
    """
    try:
        p = urlsplit(config.base_url)
        allowed = DEFAULT_HOSTS | {h.strip().lower() for h in os.getenv("RE0_LLM_ALLOWED_HOSTS", "").split(",") if h.strip()}
        if p.username or p.password or p.query or p.fragment or not p.hostname:
            raise ValueError
        if hosted and p.hostname in LOOPBACK:
            raise ModelError("托管模式不接受本机模型地址（127.0.0.1 / [::1]）：那会把公网请求转发到服务器自己的回环接口")
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
        if hosted:
            _require_public_resolution(p.hostname)
    except ValueError as exc:
        raise ModelError("模型地址不受信任：使用预设 HTTPS 主机，或显式端口的 127.0.0.1 / [::1]；自定义主机需由部署者设置 RE0_LLM_ALLOWED_HOSTS") from exc
    return config.model_copy(update={"base_url": config.base_url.rstrip("/")})


def _require_public_resolution(hostname: str) -> None:
    """Every address the name resolves to must be public, and no opt-out applies here.

    `RE0_ALLOW_LOCAL_RESOLVER` exists so a single user behind a transparent proxy can still read a
    paper. It is not a hosted-mode setting: a public service that accepts a private resolution is
    an SSRF probe with a model bill attached, so a non-public answer is refused whatever the
    environment says — and the refusal says so instead of pointing at a variable that will not help.
    """
    from ..safe_fetch import FetchError, validate_resolution

    try:
        validate_resolution(hostname, local_opt_out=False)
    except FetchError as exc:
        raise ModelError(f"托管模式无法把模型地址解析为公网主机：{exc}") from exc


def _model_entries(payload):
    """Accept the shapes providers actually use; never invent an entry."""
    if isinstance(payload, dict):
        for key in ("data", "models"):
            value = payload.get(key)
            if isinstance(value, list):
                return value
            if isinstance(value, dict) and isinstance(value.get("models"), list):
                return value["models"]
    return []


def list_models(credential: "ModelConfig | ModelListRequest", *, transport=None,
                hosted: bool = False) -> dict:
    """One bounded `GET {base_url}/models`.

    A provider that does not implement this endpoint, or a gated one, is reported
    as a failure. The caller falls back to typing a model ID; nothing is guessed.
    """
    config = validate_endpoint(credential, hosted=hosted)
    headers = {"Content-Type": "application/json"}
    key = config.api_key.get_secret_value()
    if key:
        headers["Authorization"] = f"Bearer {key}"
    try:
        with httpx.Client(transport=transport, timeout=20, follow_redirects=False, trust_env=False) as client:
            with client.stream("GET", config.base_url + "/models", headers=headers) as response:
                if response.status_code != 200:
                    # Provider bodies can echo request headers; keep our own wording.
                    raise ModelError(f"模型列表接口返回 HTTP {response.status_code}；请改用手动填写 Model ID")
                body = bytearray()
                for chunk in response.iter_bytes():
                    body.extend(chunk)
                    if len(body) > MODEL_LIST_BYTES:
                        raise ModelError("模型列表响应超过大小上限；请改用手动填写 Model ID")
    except httpx.HTTPError as exc:
        raise ModelError("无法读取模型列表；检查网络、Key 权限与该服务是否实现 GET /models，或手动填写 Model ID") from exc
    try:
        payload = json.loads(body)
    except ValueError as exc:
        raise ModelError("模型列表响应不是有效 JSON；请改用手动填写 Model ID") from exc
    found = set()
    for item in _model_entries(payload):
        name = item if isinstance(item, str) else ((item.get("id") or item.get("name")) if isinstance(item, dict) else None)
        if isinstance(name, str) and name.strip():
            found.add(name.strip()[:150])
    ordered = sorted(found)
    return {"ok": True, "models": ordered[:MODEL_LIST_LIMIT], "total": len(ordered),
            "truncated": len(ordered) > MODEL_LIST_LIMIT,
            "note": "列表只说明该服务报告了哪些模型，不代表它们支持工具调用；请用「测试工具调用」确认。"}


class ModelVault:
    """One short-lived configuration per owner, in memory.

    Nothing here is written to the database, a checkpoint, a log line or a response; `public()` is
    the only view that leaves, and it has no key in it. Snapshots carry a generation so clearing,
    expiring, or replacing a key invalidates work that already holds a copy.
    """

    def __init__(self, *, hosted: bool = False, clock=None):
        self._lock = threading.RLock()
        self._configs: dict = {}
        self._deadlines: dict[str, float] = {}
        self._expirations: dict[str, str] = {}
        self._generations: dict[str, int] = {}
        self._clock = clock or time.monotonic
        self.hosted = hosted
        self.startup_error = ""
        if os.getenv("RE0_LLM_MODEL") and os.getenv("RE0_LLM_BASE_URL"):
            try:
                self.set(ModelConfig(base_url=os.environ["RE0_LLM_BASE_URL"], model=os.environ["RE0_LLM_MODEL"],
                                     api_key=os.getenv("RE0_LLM_API_KEY", ""), trust_endpoint=True,
                                     token_parameter=os.getenv("RE0_LLM_TOKEN_PARAMETER", "max_tokens")),
                         owner=LOCAL_OWNER)
            except (ModelError, ValidationError):
                self.startup_error = "环境中的模型配置无效；请在模型设置中重新配置"

    def set(self, config, *, owner: str):
        with self._lock:
            # Validated before it replaces anything, so a refused destination leaves the previous
            # configuration in place rather than clearing it.
            validated = validate_endpoint(config, hosted=self.hosted)
            self._expire_locked(owner)
            self._generations[owner] = self._generations.get(owner, 0) + 1
            self._configs[owner] = validated
            self._deadlines[owner] = self._clock() + MODEL_KEY_TTL.total_seconds()
            self._expirations[owner] = (datetime.now(timezone.utc) + MODEL_KEY_TTL).replace(
                microsecond=0).isoformat()

    def clear(self, *, owner: str = ""):
        with self._lock:
            if owner:
                self._configs.pop(owner, None)
                self._deadlines.pop(owner, None)
                self._expirations.pop(owner, None)
                self._generations[owner] = self._generations.get(owner, 0) + 1
            else:
                # Shutdown only: dropping every owner's configuration is what `close()` means, and
                # no request path calls it without an owner.
                for existing in tuple(self._configs):
                    self._configs.pop(existing, None)
                    self._deadlines.pop(existing, None)
                    self._expirations.pop(existing, None)
                    self._generations[existing] = self._generations.get(existing, 0) + 1

    def _expire_locked(self, owner: str) -> bool:
        deadline = self._deadlines.get(owner)
        if owner in self._configs and deadline is not None and deadline <= self._clock():
            self._configs.pop(owner, None)
            self._deadlines.pop(owner, None)
            self._expirations.pop(owner, None)
            self._generations[owner] = self._generations.get(owner, 0) + 1
            return True
        return False

    def snapshot_with_generation(self, *, owner: str):
        with self._lock:
            self._expire_locked(owner)
            config = self._configs.get(owner)
            if config is None:
                raise ModelError("尚未配置模型：请先设置 Base URL、Model ID 和 API Key")
            return config.model_copy(deep=True), self._generations[owner]

    def snapshot(self, *, owner: str):
        return self.snapshot_with_generation(owner=owner)[0]

    def is_current(self, owner: str, generation: int) -> bool:
        with self._lock:
            self._expire_locked(owner)
            return owner in self._configs and self._generations.get(owner) == generation

    def configured(self, *, owner: str) -> bool:
        with self._lock:
            self._expire_locked(owner)
            return owner in self._configs

    def public(self, *, owner: str):
        with self._lock:
            self._expire_locked(owner)
            config = self._configs.get(owner)
            if config:
                return {**config.public(), "credential_expires_at": self._expirations.get(owner, "")}
            return {"configured": False, "storage": "server_memory", "credential_expires_at": "",
                    "startup_error": self.startup_error}

    def owners(self) -> list:
        """Which owners hold a configuration. Names only, for shutdown and diagnostics."""
        with self._lock:
            for owner in tuple(self._configs):
                self._expire_locked(owner)
            return sorted(self._configs)


def redact(text: str, config: ModelConfig) -> str:
    key = config.api_key.get_secret_value()
    return text.replace(key, "[REDACTED]") if key else text


class ChatModel:
    def __init__(self, config: ModelConfig, transport=None, *, hosted: bool = False):
        # Validate on construction and again before every outbound call: an allowlisted name can
        # resolve differently later, and a config may have sat in memory since it was saved.
        self.config = validate_endpoint(config, hosted=hosted)
        self.hosted = hosted
        self.transport = transport
        self._call_authorizer = None

    def set_call_authorizer(self, callback):
        """Install a last-moment check used by hosted tasks before each outbound model call."""
        self._call_authorizer = callback

    def complete(self, messages: list[dict], tools: list[dict], *, timeout=60, force_tool=None) -> dict:
        if self._call_authorizer is not None:
            self._call_authorizer()
        self.config = validate_endpoint(self.config, hosted=self.hosted)
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
                        raise ModelError(
                            f"模型接口返回 HTTP {response.status_code}；请检查权限、余额、模型名、工具调用支持和 token 参数",
                            destination=_is_destination_status(response.status_code))
                    body = bytearray()
                    for chunk in response.iter_bytes():
                        body.extend(chunk)
                        if len(body) > 2 * 1024 * 1024 or time.monotonic() > end:
                            raise ModelError("模型响应超过大小或时间上限；本次结果未采纳", destination=True)
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
            raise ModelError("模型连接或读取失败；检查网络和模型服务地址。没有自动切换服务，也没有自动重试付费请求",
                             destination=True) from exc
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
