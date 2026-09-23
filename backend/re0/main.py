"""The HTTP app: one implementation, and an identity that comes from a verified session only.

Two modes, and they are not the same product. `local` (the default) is the single-user loopback
service this has always been: no login, one implicit owner, and defences against cross-origin writes
rather than against other people, because there are no other people. `hosted` requires a session for
every API call, refuses to start until its configuration is complete, and scopes every read and write
to the verified identity's own rows.

The mode is declared, never inferred. Nothing here looks at the bind address to decide whether
authentication is needed, because "who can reach this port" is not something the process can observe
— a reverse proxy, a container port mapping and a tunnel all change the answer without changing any
configuration. What the process can do is refuse to serve hosted features without hosted
configuration, and refuse to answer as anybody when it does not know who is asking.
"""
from __future__ import annotations

import os
from contextlib import asynccontextmanager
import threading
import time
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .agent.api import agent_router
from .agent.runtime import AgentRuntime
from .agent.model import ModelError
from . import __version__
from .auth import SESSION_TTL, AccountStore, Identity, anonymous_identity, local_identity
from .db import Database
from .deployment import SESSION_COOKIE, Deployment, DeploymentError, from_env
from .models import MetadataRequest, PaperInput, ResourceAudit, ResourceInput, TopicInput
from .providers import ProviderError, check_resource, resolve_metadata
from .quota import Quota
from .resource_audit import audit_from_observation
from .service import Store, bibtex_export, validation_message
from .zotero import Connection, ZoteroClient, ZoteroError, ZoteroStore
from .zotero_sync import sync as run_zotero_sync

ROOT = Path(__file__).resolve().parents[2]
WEB = ROOT / "web"
# Routes that answer without a session: the shell has to load before anybody can log in, health has
# to answer before an orchestrator sends traffic, and login is the door. None of them returns a row
# belonging to anybody, which is what makes the list short.
OPEN_API_PATHS = frozenset({"/api/health", "/api/auth/login", "/api/auth/session"})


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


class Login(BaseModel):
    """A password, once.

    There is no owner, workspace or role field here on purpose. Everything that decides what this
    caller may see comes from the account the password belongs to, so there is nothing in the body
    for a caller to argue with.
    """

    model_config = ConfigDict(extra="forbid")
    username: str = Field(min_length=1, max_length=64)
    password: SecretStr = Field(min_length=1, max_length=200)


class ZoteroLibrary(BaseModel):
    """Which library, and nothing else. Used by the endpoints that need no credential."""

    model_config = ConfigDict(extra="forbid")
    library_type: Literal["user", "group"] = "user"
    library_id: str = Field(min_length=1, max_length=20)


class ZoteroSelection(ZoteroLibrary):
    collections: list[str] = Field(default_factory=list, max_length=20)


class ZoteroDisconnect(ZoteroLibrary):
    remove_links: bool = False


class ZoteroConnect(ZoteroLibrary):
    """One call's worth of credential.

    `SecretStr` so the key cannot ride out in a repr, a log line or an echoed validation error; the
    app-wide handler already replaces every message because model configuration carries secrets, and
    this body carries one too. It is used for the request and then dropped — the database stores the
    mapping, the cursor and the collection selection, never the key.
    """

    api_key: SecretStr
    label: str = Field(default="", max_length=200)
    collections: list[str] = Field(default_factory=list, max_length=20)
    tags: list[str] = Field(default_factory=list, max_length=10)
    max_requests: int = Field(default=60, ge=3, le=200)
    apply: bool = False


def create_app(db_path: str | None = None, transport=None, model_factory=None,
               deployment: Deployment | None = None, quota: Quota | None = None) -> FastAPI:
    # Refusing here instead of degrading is the point. A hosted deployment with no session secret has
    # no correct fallback, and choosing one silently is how an unauthenticated service ends up
    # listening on a public address.
    deployment = (deployment if deployment is not None else from_env()).require_startable()
    store = Store(Database(db_path or os.getenv("RE0_DB", str(ROOT / ".data/re0.sqlite3"))))
    accounts = AccountStore(store.db)
    # The ceilings are read once, here, so the middleware and the runtime cannot disagree about them.
    quota = quota if quota is not None else Quota.from_env(os.environ, hosted=deployment.hosted)
    agent = AgentRuntime(store, transport=transport, model_factory=model_factory,
                         deployment=deployment, quota=quota)

    @asynccontextmanager
    async def lifespan(app):
        agent.start()
        yield
        agent.close()

    app = FastAPI(title="re0 research agent", version=__version__, lifespan=lifespan)
    app.state.agent = agent
    app.state.deployment = deployment
    app.state.accounts = accounts
    app.state.quota = quota
    app.include_router(agent_router(agent))

    @app.exception_handler(ModelError)
    async def model_error(request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=422)

    @app.exception_handler(DeploymentError)
    async def deployment_error(request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=503)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, exc):
        # FastAPI normally echoes invalid input; configuration payloads contain secrets.
        return JSONResponse({"detail": [{"loc": e["loc"], "msg": "输入格式或取值不符合要求", "type": e["type"]} for e in exc.errors()]}, status_code=422)
    app.state.store = store
    # Shares the library database: a Zotero mapping points at a paper row, so the two must live in
    # one file and one backup. It holds mappings, cursors and collection selections — never a key.
    zotero = ZoteroStore(store.db)
    app.state.zotero = zotero
    zotero_guard = threading.Lock()

    def zotero_connection(data: ZoteroConnect) -> Connection:
        return Connection(library_type=data.library_type, library_id=data.library_id,
                          api_key=data.api_key.get_secret_value(), label=data.label,
                          collections=list(data.collections), tags=list(data.tags))
    audit_slots = threading.BoundedSemaphore(2)
    active_resources: set[str] = set()
    active_guard = threading.Lock()
    demo_guard = threading.Lock()
    metadata_guard = threading.Lock()
    metadata_last: dict[str, tuple[float, dict]] = {}
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=os.getenv("RE0_ALLOWED_HOSTS", "localhost,127.0.0.1,[::1],testserver").split(","))

    def session_token(request: Request) -> str:
        return request.cookies.get(SESSION_COOKIE, "")

    def identity_for(request: Request) -> Identity:
        """The identity behind this request, resolved here and nowhere else.

        In local mode there is one implicit owner and nothing to verify. In hosted mode the answer
        comes only from a token that resolves to a live, unexpired, unrevoked session — and when it
        does not, the answer is *nobody*, an identity that owns no rows. Falling back to the local
        owner here would hand a logged-out visitor the library that a single-user database was
        migrated into.
        """
        if not deployment.auth_required:
            return local_identity()
        return accounts.resolve(session_token(request)) or anonymous_identity()

    def owner_of(request: Request) -> str:
        """The owner this request acts as. Never a value from the body, the query or a header."""
        return request.state.identity.owner

    @app.middleware("http")
    async def request_guard(request: Request, call_next):
        path = request.url.path
        identity = identity_for(request) if path.startswith("/api") else None
        if identity is not None:
            # Counted before anything else is decided, so a request that is refused for another reason
            # still occupies the window it arrived in. Otherwise a flood of malformed writes would be
            # free to send, and the ceiling would only ever apply to well-formed traffic.
            gate = quota.check_request(identity.owner)
            if not gate["allowed"]:
                return JSONResponse({"detail": gate["message"]}, status_code=429,
                                    headers={"Retry-After": str(int(gate["retry_after"]) + 1)})
        if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
            client = request.headers.get("x-re0-client")
            origin = request.headers.get("origin")
            # `cli` is accepted for the local console entry points (`re0 session follow-up`), which
            # drive the same service the browser does. A browser always names its origin on a
            # cross-origin POST and sends `web` on a same-origin one, so a request claiming `cli`
            # while carrying an Origin or Referer is not the console and is refused. In hosted mode
            # the claim is refused outright: the console has no credential path there, and honouring
            # the header would widen who may write to a service that is reachable.
            if deployment.hosted and client == "cli":
                return JSONResponse({"detail": "托管模式不接受 CLI 声明的请求；请用浏览器登录后的会话"}, 403)
            if client == "cli" and (origin or request.headers.get("referer")):
                return JSONResponse({"detail": "声明为 CLI 的请求不能带浏览器来源头"}, 403)
            if client not in {"web", "cli"}:
                return JSONResponse({"detail": "写入需要 X-Re0-Client: web 或 cli 请求头"}, 403)
            if not deployment.origin_allowed(origin, request.headers.get("host", "")):
                return JSONResponse({"detail": "拒绝跨来源写入"}, 403)
            if request.headers.get("content-type", "").split(";")[0] != "application/json":
                return JSONResponse({"detail": "请使用 application/json"}, 415)
            body = bytearray()
            async for chunk in request.stream():
                body.extend(chunk)
                if len(body) > 4 * 1024 * 1024:
                    return JSONResponse({"detail": "请求体不能超过 4 MiB"}, 413)
            request._body = bytes(body)

        if deployment.auth_required and path.startswith("/api") and path not in OPEN_API_PATHS:
            if not identity.authenticated:
                # 401, not 403: nothing about this caller has been established, and the answer must
                # not differ between "no session", "expired session" and "revoked session".
                return JSONResponse({"detail": "需要登录：托管模式下每个接口都属于某一个账户"}, 401)
        request.state.identity = identity or (local_identity() if not deployment.auth_required
                                              else anonymous_identity())

        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Cache-Control"] = "no-store" if path.startswith("/api") else "no-cache"
        if not path.startswith(("/docs", "/redoc")):
            response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; form-action 'self'; base-uri 'none'"
        return response

    # --- identity: login, logout, and who am I ---

    @app.post("/api/auth/login")
    def login(data: Login):
        """Exchange a password for a session.

        There is no registration endpoint. Accounts are provisioned by the operator on the server
        console (`re0 auth create-user`), because open registration on a service that holds other
        people's model keys and library notes is not a feature anybody asked for.
        """
        if not deployment.auth_required:
            # Answering rather than silently creating an account keeps the two modes from blurring:
            # in local mode there is nothing to log into and nothing to log out of.
            raise HTTPException(409, "本地模式没有账户：所有数据属于本机唯一的所有者 local")
        user_id = accounts.verify(data.username, data.password.get_secret_value())
        if user_id is None:
            # One message for a wrong password, an unknown user, a disabled account and a locked one,
            # and the same hashing work behind all four.
            raise HTTPException(401, "用户名或口令不正确；连续失败会暂时锁定该账户")
        token, _expires = accounts.issue(user_id, note="browser")
        identity = accounts.resolve(token) or anonymous_identity()
        payload = JSONResponse({"identity": identity.public(), "mode": deployment.mode})
        # HttpOnly so page script cannot read it, SameSite=Strict so a cross-site request does not
        # carry it, Secure whenever the deployment declares an HTTPS entry. The token itself is
        # printed nowhere: not in the body, not in a log line, not in an event.
        payload.set_cookie(SESSION_COOKIE, token, max_age=int(SESSION_TTL.total_seconds()),
                           httponly=True, secure=deployment.cookie_secure, samesite="strict",
                           path="/")
        return payload

    @app.post("/api/auth/logout")
    def logout(request: Request):
        """Revoke this session. Revoked means unusable immediately, not unusable at expiry."""
        token = session_token(request)
        revoked = accounts.revoke(token) if token else False
        payload = JSONResponse({"revoked": revoked})
        payload.delete_cookie(SESSION_COOKIE, path="/")
        return payload

    @app.get("/api/auth/session")
    def session(request: Request):
        """Who is asking, and what this deployment requires. No secret and nobody's rows."""
        return {"identity": identity_for(request).public(), "deployment": deployment.describe(),
                "accounts_provisioned": accounts.count_accounts(),
                "note": ("托管模式：未登录时接口一律 401" if deployment.auth_required
                         else "本地模式：所有数据属于本机唯一所有者，没有登录这一步")}

    @app.get("/api/health")
    def health(request: Request):
        # `llm_enabled` is a per-owner fact, and health answers before anybody is authenticated. In
        # local mode there is one owner, so the answer is meaningful; in hosted mode it is not, and
        # saying `None` is better than reporting one account's configuration as the service's.
        configured = None
        if not deployment.auth_required:
            configured = agent.vault.public(owner=request.state.identity.owner)["configured"]
        return {"status": "ok", "version": __version__, "mode": deployment.mode,
                "auth_required": deployment.auth_required, "llm_enabled": configured,
                "agent_runtime": "native-durable-tool-loop",
                # Site facts only, and published on purpose: an operator deciding whether to back off,
                # and a reader who just got a 429, both need the same numbers. Nobody's own usage,
                # which is what `describe()` reports to that caller through the settings payload.
                "quota": {"mode": quota.mode, "limits": quota.limits,
                          "breaker": agent.quota.breaker.state()}}

    @app.get("/api/papers")
    def papers(request: Request):
        return store.list_papers(owner=owner_of(request))

    @app.post("/api/papers", status_code=201)
    def add_paper(data: PaperInput, request: Request):
        return store.create_paper(data, owner=owner_of(request))

    @app.get("/api/papers/{paper_id}")
    def get_paper(paper_id: str, request: Request):
        return store.get_paper(paper_id, owner=owner_of(request))

    @app.put("/api/papers/{paper_id}")
    def edit_paper(paper_id: str, data: PaperInput, request: Request):
        return store.update_paper(paper_id, data, owner=owner_of(request))

    @app.delete("/api/papers/{paper_id}", status_code=204)
    def delete_paper(paper_id: str, request: Request):
        store.delete_paper(paper_id, owner=owner_of(request))

    @app.get("/api/topics")
    def topics(request: Request):
        return store.topics(owner=owner_of(request))

    @app.post("/api/topics", status_code=201)
    def add_topic(data: TopicInput, request: Request):
        store.add_topic(data.name, owner=owner_of(request))
        return store.topics(owner=owner_of(request))

    @app.post("/api/papers/{paper_id}/resources", status_code=201)
    def add_resource(paper_id: str, data: ResourceInput, request: Request):
        return store.create_resource(paper_id, data, owner=owner_of(request))

    @app.get("/api/resources/{resource_id}/observations")
    def observations(resource_id: str, request: Request):
        return store.history(resource_id, owner=owner_of(request))

    @app.post("/api/resources/{resource_id}/confirmations", status_code=201)
    def confirm(resource_id: str, revision: dict, request: Request):
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
        return store.confirm_resource(resource_id, audit, owner=owner_of(request))

    @app.delete("/api/resources/{resource_id}", status_code=204)
    def delete_resource(resource_id: str, request: Request):
        store.delete_resource(resource_id, owner=owner_of(request))

    @app.post("/api/resources/{resource_id}/check")
    def check(resource_id: str, request: Request):
        owner = owner_of(request)
        resource = store.get_resource(resource_id, owner=owner)
        paper = store.get_paper(resource["paper_id"], owner=owner)
        if paper["is_demo"]:
            raise HTTPException(409, "演示记录不执行真实网络核验；请添加自己的论文和资源")
        with active_guard:
            if resource_id in active_resources:
                raise HTTPException(409, "这个资源正在检查，请勿重复提交")
            if not audit_slots.acquire(blocking=False):
                raise HTTPException(429, "最多同时检查 2 个资源，请稍后重试")
            active_resources.add(resource_id)
        try:
            history = store.history(resource_id, owner=owner)
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
            return {"cached": False,
                    "observation": store.save_observation(resource_id, observation, owner=owner)}
        finally:
            with active_guard:
                active_resources.discard(resource_id)
                audit_slots.release()

    @app.post("/api/metadata/resolve")
    def metadata(data: MetadataRequest):
        # Bounded process-local cache. No notes or attachments leave the machine. The cache is keyed
        # by identifier and holds published bibliographic metadata — nothing that belongs to one
        # owner rather than another, which is the only reason it is not scoped.
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
    def import_csl(data: CslImport, request: Request):
        return store.import_csl(data.items, data.dry_run, owner=owner_of(request))

    @app.post("/api/import/resource-audits")
    def import_resource_audits(data: AuditImport, request: Request):
        return store.import_audits(data.items, data.dry_run, owner=owner_of(request))

    # --- Zotero: read-only, preview by default, and the key lives only for the length of a call ---

    @app.get("/api/zotero/status")
    def zotero_status(request: Request,
                      library_type: str = Query("user", pattern="^(user|group)$"),
                      library_id: str = Query(min_length=1, max_length=20)):
        # Needs no Zotero credential: what is mapped, where the cursor stands, which collections are
        # selected. Contains no key and no note text. It is still scoped — a mapping belongs to the
        # reader who made it, and two readers may sync the same library into their own.
        return zotero.status(Connection(library_type=library_type, library_id=library_id),
                             owner=owner_of(request))

    @app.get("/api/zotero/links")
    def zotero_links(request: Request, paper_id: str = Query("", max_length=80)):
        return zotero.links(paper_id=paper_id, owner=owner_of(request))

    @app.post("/api/zotero/collections")
    def zotero_collections(data: ZoteroConnect, request: Request):
        """Read the remote collection list — names and keys only, never their contents."""
        connection = zotero_connection(data)
        try:
            with ZoteroClient(connection, transport=transport,
                              max_requests=min(data.max_requests, 10)) as client:
                rows = client.collections()
        except ZoteroError as exc:
            raise HTTPException(502, str(exc)) from exc
        written = zotero.save_collections(connection, rows, owner=owner_of(request),
                                          selected=list(data.collections) or None)
        return {"connection": connection.public(), "collections": rows, "stored": written,
                "note": "只读了集合名称与 key；集合内容要等同步时按条目读取"}

    @app.put("/api/zotero/selection")
    def zotero_selection(data: ZoteroSelection, request: Request):
        """Choosing which collections a sync covers is a scope decision, so it is explicit."""
        return zotero.select_collections(
            Connection(library_type=data.library_type, library_id=data.library_id),
            list(data.collections), owner=owner_of(request))

    @app.post("/api/zotero/sync")
    def zotero_sync_endpoint(data: ZoteroConnect, request: Request):
        """Preview by default; `apply: true` commits. One call, one transaction, one cursor move.

        Serialized on a lock: two concurrent syncs of the same library would each read the same
        window and race on the cursor, and the loser's plan would be stale. Refusing the second is
        cheaper than explaining it afterwards.
        """
        owner = owner_of(request)
        if not zotero_guard.acquire(blocking=False):
            raise HTTPException(409, "已有一个 Zotero 同步在进行；请等它结束，不要并发同步同一个库")
        try:
            return run_zotero_sync(zotero, store.list_papers(owner=owner), zotero_connection(data),
                                   owner=owner, transport=transport, apply=data.apply,
                                   max_requests=data.max_requests)
        except ZoteroError as exc:
            raise HTTPException(502, str(exc)) from exc
        finally:
            zotero_guard.release()

    @app.post("/api/zotero/disconnect")
    def zotero_disconnect(data: ZoteroDisconnect, request: Request):
        """Forget the cursor and the selection. Papers, notes, resources and evidence are never
        touched, with or without `remove_links`."""
        return zotero.disconnect(Connection(library_type=data.library_type,
                                            library_id=data.library_id),
                                 owner=owner_of(request), remove_links=data.remove_links)

    @app.get("/api/export")
    def export(request: Request, format: str = Query("json", pattern="^(json|bibtex)$")):
        owner = owner_of(request)
        if format == "bibtex":
            return Response(bibtex_export(store.list_papers(owner=owner)),
                            media_type="application/x-bibtex",
                            headers={"Content-Disposition": 'attachment; filename="re0-library.bib"'})
        return JSONResponse(store.export(owner=owner),
                            headers={"Content-Disposition": 'attachment; filename="re0-library.json"'})

    @app.post("/api/demo")
    def seed_demo(request: Request):
        with demo_guard:
            return {"created": store.seed_demo(owner=owner_of(request))}

    @app.delete("/api/demo")
    def clear_demo(request: Request):
        with demo_guard:
            return {"deleted": store.clear_demo(owner=owner_of(request))}

    @app.get("/")
    def index():
        return FileResponse(WEB / "agent.html")

    @app.get("/library")
    def library_index():
        return FileResponse(WEB / "index.html")

    app.mount("/static", StaticFiles(directory=WEB), name="static")
    return app


app = create_app()
