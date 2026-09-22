"""Single-user local app. Do not expose this unauthenticated service publicly."""
from __future__ import annotations

import os
from contextlib import asynccontextmanager
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .agent.api import agent_router
from .agent.runtime import AgentRuntime
from .agent.model import ModelError
from . import __version__
from .db import Database
from .models import MetadataRequest, PaperInput, ResourceAudit, ResourceInput, TopicInput
from .providers import ProviderError, check_resource, resolve_metadata
from .resource_audit import audit_from_observation
from .service import Store, bibtex_export, validation_message

ROOT = Path(__file__).resolve().parents[2]
WEB = ROOT / "web"


class CslImport(BaseModel):
    model_config = ConfigDict(extra="forbid")
    items: list[dict] = Field(max_length=500)
    dry_run: bool = True


class AuditImport(BaseModel):
    """A preview by default, like the CSL import: an approval writes to the library, so the caller
    sees what it would link before it does."""

    model_config = ConfigDict(extra="forbid")
    items: list[dict] = Field(max_length=200)
    dry_run: bool = True


def create_app(db_path: str | None = None, transport=None, model_factory=None) -> FastAPI:
    store = Store(Database(db_path or os.getenv("RE0_DB", str(ROOT / ".data/re0.sqlite3"))))
    agent = AgentRuntime(store, transport=transport, model_factory=model_factory)

    @asynccontextmanager
    async def lifespan(app):
        agent.start()
        yield
        agent.close()

    app = FastAPI(title="re0 research agent", version=__version__, lifespan=lifespan)
    app.state.agent = agent
    app.include_router(agent_router(agent))

    @app.exception_handler(ModelError)
    async def model_error(request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=422)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, exc):
        # FastAPI normally echoes invalid input; configuration payloads contain secrets.
        return JSONResponse({"detail": [{"loc": e["loc"], "msg": "输入格式或取值不符合要求", "type": e["type"]} for e in exc.errors()]}, status_code=422)
    app.state.store = store
    audit_slots = threading.BoundedSemaphore(2)
    active_resources: set[str] = set()
    active_guard = threading.Lock()
    demo_guard = threading.Lock()
    metadata_guard = threading.Lock()
    metadata_last: dict[str, tuple[float, dict]] = {}
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=os.getenv("RE0_ALLOWED_HOSTS", "localhost,127.0.0.1,[::1],testserver").split(","))

    @app.middleware("http")
    async def local_security(request: Request, call_next):
        if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
            client = request.headers.get("x-re0-client")
            origin = request.headers.get("origin")
            # `cli` is accepted for the local console entry points (`re0 session follow-up`), which
            # drive the same service the browser does. A browser always names its origin on a
            # cross-origin POST and sends `web` on a same-origin one, so a request claiming `cli`
            # while carrying an Origin or Referer is not the console and is refused. This widens who
            # may write to a loopback service that is already unauthenticated; it does not make that
            # service safe to expose, and SECURITY.md still says not to.
            if client == "cli" and (origin or request.headers.get("referer")):
                return JSONResponse({"detail": "声明为 CLI 的请求不能带浏览器来源头"}, 403)
            if client not in {"web", "cli"}:
                return JSONResponse({"detail": "写入需要 X-Re0-Client: web 或 cli 请求头"}, 403)
            if origin:
                try:
                    parsed = urlsplit(origin)
                    valid = parsed.scheme in {"http", "https"} and parsed.netloc == request.headers.get("host")
                except ValueError:
                    valid = False
                if not valid:
                    return JSONResponse({"detail": "拒绝跨来源写入"}, 403)
            if request.headers.get("content-type", "").split(";")[0] != "application/json":
                return JSONResponse({"detail": "请使用 application/json"}, 415)
            body = bytearray()
            async for chunk in request.stream():
                body.extend(chunk)
                if len(body) > 4 * 1024 * 1024:
                    return JSONResponse({"detail": "请求体不能超过 4 MiB"}, 413)
            request._body = bytes(body)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Cache-Control"] = "no-store" if request.url.path.startswith("/api") else "no-cache"
        if not request.url.path.startswith(("/docs", "/redoc")):
            response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; form-action 'self'; base-uri 'none'"
        return response

    @app.get("/api/health")
    def health():
        return {"status": "ok", "version": __version__, "mode": "local-single-user", "llm_enabled": agent.vault.public()["configured"], "agent_runtime": "native-durable-tool-loop"}

    @app.get("/api/papers")
    def papers():
        return store.list_papers()

    @app.post("/api/papers", status_code=201)
    def add_paper(data: PaperInput):
        return store.create_paper(data)

    @app.get("/api/papers/{paper_id}")
    def get_paper(paper_id: str):
        return store.get_paper(paper_id)

    @app.put("/api/papers/{paper_id}")
    def edit_paper(paper_id: str, data: PaperInput):
        return store.update_paper(paper_id, data)

    @app.delete("/api/papers/{paper_id}", status_code=204)
    def delete_paper(paper_id: str):
        store.delete_paper(paper_id)

    @app.get("/api/topics")
    def topics():
        return store.topics()

    @app.post("/api/topics", status_code=201)
    def add_topic(data: TopicInput):
        store.add_topic(data.name)
        return store.topics()

    @app.post("/api/papers/{paper_id}/resources", status_code=201)
    def add_resource(paper_id: str, data: ResourceInput):
        return store.create_resource(paper_id, data)

    @app.get("/api/resources/{resource_id}/observations")
    def observations(resource_id: str):
        return store.history(resource_id)

    @app.post("/api/resources/{resource_id}/confirmations", status_code=201)
    def confirm(resource_id: str, revision: dict):
        # A person confirming what a check found. Stored beside the observations rather than over
        # them, so the machine's reading and the human's stay separately retrievable.
        #
        # Validated here instead of by the request parser, so the refusal can say which source was
        # missing: the app-wide handler hides every validation message because model-configuration
        # payloads carry secrets, and this body carries none.
        try:
            audit = ResourceAudit.model_validate(revision)
        except ValidationError as exc:
            raise HTTPException(422, validation_message(exc)) from exc
        return store.confirm_resource(resource_id, audit)

    @app.delete("/api/resources/{resource_id}", status_code=204)
    def delete_resource(resource_id: str):
        store.delete_resource(resource_id)

    @app.post("/api/resources/{resource_id}/check")
    def check(resource_id: str):
        resource = store.get_resource(resource_id)
        paper = store.get_paper(resource["paper_id"])
        if paper["is_demo"]:
            raise HTTPException(409, "演示记录不执行真实网络核验；请添加自己的论文和资源")
        with active_guard:
            if resource_id in active_resources:
                raise HTTPException(409, "这个资源正在检查，请勿重复提交")
            if not audit_slots.acquire(blocking=False):
                raise HTTPException(429, "最多同时检查 2 个资源，请稍后重试")
            active_resources.add(resource_id)
        try:
            history = store.history(resource_id)
            if history:
                from datetime import datetime, timezone
                age = (datetime.now(timezone.utc) - datetime.fromisoformat(history[0]["checked_at"])).total_seconds()
                if age < 60:
                    return {"cached": True, "observation": history[0]}
            checked = check_resource(resource["url"], transport)
            observation = checked.model_dump(mode="json")
            observation["paper_version_snapshot"] = {"arxiv_id": paper["arxiv_id"], "version_label": paper["version_label"], "doi": paper["doi"]}
            # The field-level reading travels with the raw check, so the library's history can
            # answer "which artifact classes does this resource cover" and not only "what did the
            # provider say". Additive: the fields already stored keep their meaning.
            observation["resource_audit"] = audit_from_observation(
                resource["url"], checked,
                candidate={"url": resource["url"], "origin": "库内资源"},
                paper={"title": paper["title"], "abstract": "", "arxiv_id": paper["arxiv_id"],
                       "doi": paper["doi"]},
                publication={"venue": paper.get("venue", "")}).model_dump(mode="json")
            return {"cached": False, "observation": store.save_observation(resource_id, observation)}
        finally:
            with active_guard:
                active_resources.discard(resource_id)
                audit_slots.release()

    @app.post("/api/metadata/resolve")
    def metadata(data: MetadataRequest):
        # Bounded process-local cache. No notes or attachments leave the machine.
        with metadata_guard:
            item = metadata_last.get(data.identifier)
            if item and time.monotonic() - item[0] < 3600:
                return item[1]
        try:
            result = resolve_metadata(data.identifier, transport).model_dump(mode="json")
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        except ProviderError as exc:
            raise HTTPException(502, str(exc)) from exc
        with metadata_guard:
            if len(metadata_last) >= 128:
                metadata_last.pop(next(iter(metadata_last)))
            metadata_last[data.identifier] = (time.monotonic(), result)
        return result

    @app.post("/api/import/csl")
    def import_csl(data: CslImport):
        return store.import_csl(data.items, data.dry_run)

    @app.post("/api/import/resource-audits")
    def import_resource_audits(data: AuditImport):
        return store.import_audits(data.items, data.dry_run)

    @app.get("/api/export")
    def export(format: str = Query("json", pattern="^(json|bibtex)$")):
        if format == "bibtex":
            return Response(bibtex_export(store.list_papers()), media_type="application/x-bibtex",
                            headers={"Content-Disposition": 'attachment; filename="re0-library.bib"'})
        return JSONResponse(store.export(), headers={"Content-Disposition": 'attachment; filename="re0-library.json"'})

    @app.post("/api/demo")
    def seed_demo():
        with demo_guard:
            return {"created": store.seed_demo()}

    @app.delete("/api/demo")
    def clear_demo():
        with demo_guard:
            return {"deleted": store.clear_demo()}

    @app.get("/")
    def index():
        return FileResponse(WEB / "agent.html")

    @app.get("/library")
    def library_index():
        return FileResponse(WEB / "index.html")

    app.mount("/static", StaticFiles(directory=WEB), name="static")
    return app


app = create_app()
