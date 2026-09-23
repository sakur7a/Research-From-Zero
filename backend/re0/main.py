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
import sys
from contextlib import asynccontextmanager
import threading
import time
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError, field_validator
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .agent.api import agent_router
from .agent.runtime import AgentRuntime
from .agent.model import ModelError
from . import __version__
from .auth import (GUEST_SESSION_TTL, SESSION_TTL, AccountStore, GuestCapacityError,
                   Identity, anonymous_identity, local_identity)
from .db import Database
from .deployment import SESSION_COOKIE, Deployment, DeploymentError, from_env, trusted_hosts_from_env
from .models import (MetadataRequest, PaperInput, ResearchRelationInput,
                     ResearchTemplateDefinition, ResourceAudit, ResourceInput,
                     TopicAssignmentInput, TopicInput)
from .paths import default_database, web_directory
from .providers import ProviderError, check_resource, resolve_metadata
from .quota import Quota
from .resource_audit import audit_from_observation
from .service import Store, bibtex_export, validation_message
from .zotero import Connection, ZoteroClient, ZoteroError, ZoteroStore
from .zotero_sync import sync as run_zotero_sync
from .workspace import (Workspace, WorkspaceError, managed_workspace_path,
                        managed_workspaces_root, delete_managed_workspaces)

# Routes that answer without a session: the shell has to load before anybody can log in, health has
# to answer before an orchestrator sends traffic, and login is the door. None of them returns a row
# belonging to anybody, which is what makes the list short.
OPEN_API_PATHS = frozenset({"/api/health", "/api/auth/login", "/api/auth/session",
                            "/api/auth/guest"})


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


class WorkspaceBundleInput(BaseModel):
    """A bounded source bundle; it can add source snapshots but never approve a paper."""

    model_config = ConfigDict(extra="forbid")
    bundle: dict


class FullTextWorkspaceImport(BaseModel):
    """A selected set of source IDs; the server derives both owner and workspace path."""

    model_config = ConfigDict(extra="forbid")
    workspace_id: str = Field(pattern=r"^ws_[0-9a-f]{16}$")
    source_ids: list[str] = Field(min_length=1, max_length=200)
    paper_version_id: str = Field(min_length=1, max_length=100)

    @field_validator("source_ids")
    @classmethod
    def distinct_sources(cls, values: list[str]) -> list[str]:
        if len(set(values)) != len(values):
            raise ValueError("同一 source ID 不能在一次导入中重复选择")
        return values


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
    # Resolved per app, not once at import: the directory is a property of the installation, and a
    # test (or a second app in one process) may legitimately point at a different one.
    web = web_directory()
    store = Store(Database(db_path or os.getenv("RE0_DB") or str(default_database())))
    accounts = AccountStore(store.db)
    # The ceilings are read once, here, so the middleware and the runtime cannot disagree about them.
    quota = quota if quota is not None else Quota.from_env(os.environ, hosted=deployment.hosted)
    agent = AgentRuntime(store, transport=transport, model_factory=model_factory,
                         deployment=deployment, quota=quota,
                         owner_has_live_session=(accounts.owner_has_live_session
                                                 if deployment.auth_required else None))
    guest_cleanup_lock = threading.Lock()
    deleting_guest_owners: set[str] = set()
    guest_request_counts: dict[str, int] = {}
    guest_cleanup_stop = threading.Event()

    def cleanup_guest_owner(owner: str) -> bool:
        """Clear one expired guest only after its current task has reached a safe boundary."""
        if not owner:
            return False
        with guest_cleanup_lock:
            if owner in deleting_guest_owners:
                return False
            deleting_guest_owners.add(owner)
            try:
                agent.revoke_owner(owner)
                if guest_request_counts.get(owner, 0) or agent.busy_view(owner)["queue"]["mine"]:
                    return False
                store.delete_owner_data(owner=owner)
                delete_managed_workspaces(store.db.path, owner)
                accounts.delete_guest_owner(owner)
                return True
            finally:
                deleting_guest_owners.discard(owner)

    def sweep_expired_guests():
        while not guest_cleanup_stop.wait(30):
            for guest in accounts.expired_guest_sessions():
                try:
                    cleanup_guest_owner(guest["workspace"])
                except Exception as exc:  # noqa: BLE001 - keep cleanup alive for the next bounded pass
                    # No owner, row contents, path or key is written to logs.
                    print(f"guest cleanup deferred ({type(exc).__name__})", file=sys.stderr)

    @asynccontextmanager
    async def lifespan(app):
        agent.start()
        sweeper = None
        if deployment.guest_access_enabled:
            sweeper = threading.Thread(target=sweep_expired_guests, name="re0-guest-cleanup", daemon=True)
            sweeper.start()
        try:
            yield
        finally:
            guest_cleanup_stop.set()
            if sweeper is not None:
                sweeper.join(timeout=2)
            agent.close()

    app = FastAPI(title="re0 research agent", version=__version__, lifespan=lifespan)
    app.state.agent = agent
    app.state.deployment = deployment
    app.state.accounts = accounts
    app.state.quota = quota
    app.include_router(agent_router(agent, store))

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
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=trusted_hosts_from_env())

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
        identity = accounts.resolve(session_token(request)) or anonymous_identity()
        if identity.kind == "guest" and identity.owner in deleting_guest_owners:
            return anonymous_identity()
        return identity

    def owner_of(request: Request) -> str:
        """The owner this request acts as. Never a value from the body, the query or a header."""
        identity = request.state.identity
        if identity.kind == "guest" and not accounts.owner_has_live_session(identity.owner):
            raise HTTPException(401, "访客会话已结束；本次数据正在清理")
        return identity.owner

    def workspaces_for(owner: str) -> dict:
        """List only this owner's server-managed workspaces; no caller-supplied paths are read."""
        root = managed_workspaces_root(store.db.path, owner)
        rows = []
        if not root.is_dir():
            return {"workspaces": rows}
        for path in sorted(root.iterdir(), key=lambda item: item.name):
            if path.is_symlink() or not path.is_dir():
                continue
            try:
                workspace = Workspace(path).open(create=False)
            except (WorkspaceError, OSError):
                rows.append({"workspace_id": path.name, "available": False,
                             "total_sources": 0, "sources": []})
                continue
            sources = []
            identifiers = workspace.identifiers()
            for identifier in identifiers[:200]:
                try:
                    document = workspace.read(identifier)
                except (WorkspaceError, OSError):
                    continue
                paper = document.get("paper") if isinstance(document.get("paper"), dict) else {}
                fulltext = document.get("fulltext") if isinstance(document.get("fulltext"), dict) else {}
                fulltext_summary = ({key: fulltext.get(key) for key in (
                    "source", "identifier", "version", "state", "parser", "parser_version",
                    "fetched_at", "parse_quality")} if fulltext else None)
                sources.append({"source_id": identifier, "title": str(paper.get("title") or ""),
                                "source_url": str(document.get("source_url") or ""),
                                "locator": str(document.get("locator") or ""),
                                "kind": str(document.get("kind") or ""),
                                "retrieved_at": str(document.get("retrieved_at") or ""),
                                "fulltext": fulltext_summary,
                                "imported_by_user": bool(document.get("imported_by_user"))})
            rows.append({"workspace_id": workspace.workspace_id, "available": True,
                         "total_sources": len(identifiers), "sources": sources})
        return {"workspaces": rows}

    def import_workspace_bundle(data: WorkspaceBundleInput, request: Request, *, apply: bool):
        owner = owner_of(request)
        workspace_id = data.bundle.get("workspace_id") if isinstance(data.bundle, dict) else ""
        try:
            root = managed_workspace_path(store.db.path, owner, workspace_id)
            workspace, result = Workspace.import_to(root, data.bundle, apply=apply)
        except (WorkspaceError, OSError) as exc:
            raise HTTPException(422, f"工作区 bundle 无法处理：{exc}") from exc
        return {**result, "workspace_id": workspace.workspace_id}

    def import_fulltext_sources(paper_id: str, data: FullTextWorkspaceImport,
                                request: Request, *, apply: bool):
        owner = owner_of(request)
        try:
            root = managed_workspace_path(store.db.path, owner, data.workspace_id)
            workspace = Workspace(root).open(create=False, workspace_id=data.workspace_id)
            sources = [(source_id, workspace.read(source_id)) for source_id in data.source_ids]
        except (WorkspaceError, OSError) as exc:
            # The same refusal covers a foreign workspace and a source ID it does not hold.
            raise HTTPException(404, "工作区或所选来源不存在") from exc
        return store.import_fulltext_chunks(paper_id, data.paper_version_id, sources,
                                           owner=owner, apply=apply)

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

        tracked_guest_owner = ""
        cleanup_request = ((path == "/api/auth/guest/data" and request.method == "DELETE")
                           or (path == "/api/auth/logout" and request.method == "POST"))
        if identity is not None and identity.kind == "guest" and not cleanup_request:
            # Register immediately before the route runs. A delete/expiry cleanup that races a request
            # either sees it in this count or revokes first and causes this request to be refused.
            with guest_cleanup_lock:
                if (identity.owner in deleting_guest_owners
                        or not accounts.owner_has_live_session(identity.owner)):
                    identity = anonymous_identity()
                else:
                    tracked_guest_owner = identity.owner
                    guest_request_counts[tracked_guest_owner] = (
                        guest_request_counts.get(tracked_guest_owner, 0) + 1)
        if deployment.auth_required and path.startswith("/api") and path not in OPEN_API_PATHS:
            if not identity.authenticated:
                # 401, not 403: nothing about this caller has been established, and the answer must
                # not differ between "no session", "expired session" and "revoked session".
                return JSONResponse({"detail": "需要登录：托管模式下每个接口都属于某一个账户"}, 401)
        request.state.identity = identity or (local_identity() if not deployment.auth_required
                                              else anonymous_identity())

        try:
            response = await call_next(request)
        finally:
            if tracked_guest_owner:
                with guest_cleanup_lock:
                    remaining = guest_request_counts.get(tracked_guest_owner, 0) - 1
                    if remaining > 0:
                        guest_request_counts[tracked_guest_owner] = remaining
                    else:
                        guest_request_counts.pop(tracked_guest_owner, None)
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
        workspace = accounts.workspace_for(user_id)
        if workspace and not accounts.owner_has_live_session(workspace):
            # A prior operator revoke or an expired final session invalidates the old memory copy.
            # A second live browser session does not clear credentials another tab is using.
            agent.revoke_owner(workspace)
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

    @app.post("/api/auth/guest")
    def create_guest(request: Request):
        """Issue a distinct short-lived owner only when the deployment explicitly enables guests."""
        if not deployment.hosted or not deployment.guest_access_enabled:
            raise HTTPException(404, "此部署未开放临时访客会话")
        current = request.state.identity
        if current.authenticated:
            if current.kind == "guest":
                return {"identity": current.public(), "deployment": deployment.describe(),
                        "existing_session": True}
            raise HTTPException(409, "当前浏览器已登录账户；请先退出再开始临时访客会话")
        # Bound retained data before admitting another session. Expired/revoked guests with a running
        # request stay counted until cleanup reaches that request's next safe boundary.
        for guest in accounts.expired_guest_sessions():
            cleanup_guest_owner(guest["workspace"])
        try:
            identity, token = accounts.create_guest(ttl=GUEST_SESSION_TTL)
        except GuestCapacityError as exc:
            raise HTTPException(429, str(exc), headers={"Retry-After": "30"}) from exc
        payload = JSONResponse({"identity": identity.public(), "deployment": deployment.describe(),
                                "existing_session": False})
        payload.set_cookie(SESSION_COOKIE, token, max_age=int(GUEST_SESSION_TTL.total_seconds()),
                           httponly=True, secure=deployment.cookie_secure, samesite="strict", path="/")
        return payload

    @app.delete("/api/auth/guest/data")
    def delete_guest_data(request: Request):
        """Explicitly delete this guest's library, task/evidence rows and imported source files."""
        identity = request.state.identity
        if identity.kind != "guest":
            raise HTTPException(404, "当前会话不是临时访客会话")
        accounts.revoke_guest_owner(identity.owner)
        deleted = cleanup_guest_owner(identity.owner)
        payload = JSONResponse({"deleted": deleted, "pending": not deleted,
                                "note": ("本次访客数据已删除" if deleted else
                                         "任务正在停止；会话已失效，数据将在安全边界后清理")},
                               status_code=200 if deleted else 202)
        payload.delete_cookie(SESSION_COOKIE, path="/")
        return payload

    @app.post("/api/auth/logout")
    def logout(request: Request):
        """Revoke this session. Revoked means unusable immediately, not unusable at expiry."""
        token = session_token(request)
        revoked = accounts.revoke(token) if token else False
        identity = request.state.identity
        credentials_cleared = (agent.revoke_owner(identity.owner)
                               if deployment.auth_required and identity.authenticated else False)
        data_deleted = False
        if identity.kind == "guest":
            data_deleted = cleanup_guest_owner(identity.owner)
        payload = JSONResponse({"revoked": revoked, "credentials_cleared": credentials_cleared,
                                "guest_data_deleted": data_deleted,
                                "guest_cleanup_pending": identity.kind == "guest" and not data_deleted})
        payload.delete_cookie(SESSION_COOKIE, path="/")
        return payload

    @app.get("/api/auth/session")
    def session(request: Request):
        """Who is asking, and what this deployment requires. No secret and nobody's rows."""
        identity = request.state.identity
        if identity.kind == "guest":
            note = "临时访客会话：本次数据独立隔离，最多保留 2 小时；可随时删除"
        elif deployment.guest_access_enabled:
            note = "无需管理员开户：可创建独立的短期访客会话；实时任务需要自己的模型 Key"
        else:
            note = ("托管模式：未登录时接口一律 401" if deployment.auth_required
                    else "本地模式：所有数据属于本机唯一所有者，没有登录这一步")
        return {"identity": identity.public(), "deployment": deployment.describe(),
                "accounts_provisioned": accounts.count_accounts(),
                "note": note}

    @app.get("/api/health")
    def health(request: Request):
        # `llm_enabled` is a per-owner fact, and health answers before anybody is authenticated. In
        # local mode there is one owner, so the answer is meaningful; in hosted mode it is not, and
        # saying `None` is better than reporting one account's configuration as the service's.
        configured = None
        if not deployment.auth_required:
            configured = agent.vault.public(owner=request.state.identity.owner)["configured"]
        return {"status": "ok", "version": __version__, "mode": deployment.mode,
                "auth_required": deployment.auth_required, "storage_mode": deployment.storage_mode,
                "llm_enabled": configured,
                "agent_runtime": "native-durable-tool-loop",
                # Site facts only, and published on purpose: an operator deciding whether to back off,
                # and a reader who just got a 429, both need the same numbers. Nobody's own usage,
                # which is what `describe()` reports to that caller through the settings payload.
                "quota": {"mode": quota.mode, "limits": quota.limits,
                          "breaker": "open" if quota.emergency_stop else "closed"}}

    @app.get("/api/papers")
    def papers(request: Request):
        return store.list_papers(owner=owner_of(request))

    @app.post("/api/papers", status_code=201)
    def add_paper(data: PaperInput, request: Request):
        return store.create_paper(data, owner=owner_of(request))

    @app.get("/api/papers/{paper_id}")
    def get_paper(paper_id: str, request: Request):
        return store.get_paper(paper_id, owner=owner_of(request))

    @app.get("/api/papers/{paper_id}/knowledge")
    def paper_knowledge(paper_id: str, request: Request):
        return store.knowledge(paper_id, owner=owner_of(request))

    @app.post("/api/papers/{paper_id}/knowledge/fulltext/preview")
    def preview_fulltext_import(paper_id: str, data: FullTextWorkspaceImport, request: Request):
        return import_fulltext_sources(paper_id, data, request, apply=False)

    @app.post("/api/papers/{paper_id}/knowledge/fulltext/import")
    def confirm_fulltext_import(paper_id: str, data: FullTextWorkspaceImport, request: Request):
        return import_fulltext_sources(paper_id, data, request, apply=True)

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

    @app.get("/api/knowledge/templates")
    def knowledge_templates(request: Request):
        return store.templates(owner=owner_of(request))

    @app.post("/api/knowledge/templates/{template_key}", status_code=201)
    def create_knowledge_template(template_key: str, data: ResearchTemplateDefinition,
                                  request: Request):
        return store.create_template_version(template_key, data, owner=owner_of(request))

    @app.post("/api/papers/{paper_id}/topic-assignments", status_code=201)
    def assign_template_topic(paper_id: str, data: TopicAssignmentInput, request: Request):
        return store.assign_template_topic(paper_id, data, owner=owner_of(request))

    @app.delete("/api/papers/{paper_id}/topic-assignments/{assignment_id}", status_code=204)
    def remove_template_topic(paper_id: str, assignment_id: str, request: Request):
        store.remove_topic_assignment(paper_id, assignment_id, owner=owner_of(request))

    @app.post("/api/papers/{paper_id}/relations", status_code=201)
    def add_research_relation(paper_id: str, data: ResearchRelationInput, request: Request):
        return store.add_relation(paper_id, data, owner=owner_of(request))

    @app.get("/api/knowledge/relations")
    def knowledge_relations(request: Request,
                            paper_id: str = Query("", max_length=80),
                            relation_type: str = Query("", pattern="^(|uses_method|evaluated_on|has_resource|claim)$"),
                            q: str = Query("", max_length=200),
                            limit: int = Query(50, ge=1, le=100),
                            offset: int = Query(0, ge=0, le=10000)):
        return store.list_relations(owner=owner_of(request), paper_id=paper_id,
                                    relation_type=relation_type, query=q,
                                    limit=limit, offset=offset)

    @app.get("/api/workspaces")
    def list_workspaces(request: Request):
        return workspaces_for(owner_of(request))

    @app.get("/api/workspaces/{workspace_id}/export")
    def export_workspace(workspace_id: str, request: Request):
        owner = owner_of(request)
        try:
            root = managed_workspace_path(store.db.path, owner, workspace_id)
            if not root.is_dir() or not (root / ".re0-workspace.json").is_file():
                raise WorkspaceError("工作区不存在")
            workspace = Workspace(root).open(create=False, workspace_id=workspace_id)
            bundle = workspace.bundle()
        except (WorkspaceError, OSError) as exc:
            raise HTTPException(404, "工作区不存在或无法导出") from exc
        return JSONResponse(bundle, headers={"Content-Disposition":
                                            f'attachment; filename="re0-workspace-{workspace_id}.json"'})

    @app.post("/api/workspaces/import/preview")
    def preview_workspace_import(data: WorkspaceBundleInput, request: Request):
        return import_workspace_bundle(data, request, apply=False)

    @app.post("/api/workspaces/import", status_code=201)
    def import_workspace(data: WorkspaceBundleInput, request: Request):
        return import_workspace_bundle(data, request, apply=True)

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
        return FileResponse(web / "agent.html")

    @app.get("/login")
    def login_page():
        """The door itself: a shell that asks the open endpoint who it is talking to.

        Served in both modes, because the page's first job is to find out which mode this is — and in
        local mode the honest answer is that there is nothing to log into, which is worth saying out
        loud rather than 404ing on a link every page carries.
        """
        return FileResponse(web / "login.html")

    @app.get("/library")
    def library_index():
        return FileResponse(web / "index.html")

    app.mount("/static", StaticFiles(directory=web), name="static")
    return app


app = create_app()
