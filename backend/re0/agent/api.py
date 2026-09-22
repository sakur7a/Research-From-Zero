"""Task APIs expose progress and source evidence, never model credentials/checkpoints.

Every route here takes the request only to read one thing from it: the identity the middleware
verified. The owner is never a parameter a caller can send, because a parameter is something a caller
can also change.
"""
from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse

from .model import ModelError
from .schemas import (Approval, FollowUpInput, ModelConfig, ModelListRequest, RetryInput, SessionCaps,
                      TaskDefaults, TaskInput)


def agent_router(runtime):
    router = APIRouter(prefix="/api/agent", tags=["Research agent"])

    def owner(request: Request) -> str:
        return request.state.identity.owner

    @router.get("/config")
    def config(request: Request):
        return runtime.public(owner=owner(request))

    @router.put("/config")
    def configure(data: ModelConfig, request: Request):
        return runtime.configure(data, owner=owner(request))

    @router.delete("/config")
    def clear(request: Request):
        return runtime.configure(None, owner=owner(request))

    @router.post("/config/test")
    def test(request: Request):
        return runtime.test_connection(owner=owner(request))

    @router.post("/models")
    def models(data: ModelListRequest):
        # Read-only probe used by the settings picker. The key is not persisted, and the probe is
        # scoped to the credentials in this request rather than to any stored configuration.
        return runtime.list_models(data)

    @router.put("/defaults")
    def defaults(data: TaskDefaults, request: Request):
        # This owner's defaults for new tasks; not credentials, so they are readable again.
        return runtime.set_defaults(data, owner=owner(request))

    @router.put("/session-defaults")
    def session_defaults(data: SessionCaps, request: Request):
        """Ceilings for conversations this owner creates from now on. Existing ones keep their own."""
        return runtime.set_session_caps(data, owner=owner(request))

    @router.get("/runs")
    def tasks(request: Request):
        return runtime.tasks.list(owner=owner(request))

    @router.post("/runs", status_code=202)
    def start(data: TaskInput, request: Request):
        return runtime.submit(data, owner=owner(request))

    @router.get("/runs/{run_id}")
    def task(run_id: str, request: Request):
        return runtime.tasks.get(run_id, owner=owner(request))

    @router.get("/runs/{run_id}/events")
    def events(run_id: str, request: Request, after: int = Query(0, ge=0)):
        return runtime.tasks.events(run_id, after, owner=owner(request))

    @router.post("/runs/{run_id}/cancel")
    def cancel(run_id: str, request: Request):
        runtime.tasks.request_cancel(run_id, owner=owner(request))
        return {"requested": True, "note": "在当前调用结束/超时后的下一安全边界停止"}

    @router.post("/runs/{run_id}/resume", status_code=202)
    def resume(run_id: str, data: Approval, request: Request):
        return runtime.resume(run_id, owner=owner(request))

    @router.get("/conversations")
    def conversations(request: Request):
        # A conversation is the line of turns that shares evidence and one cumulative ledger.
        return runtime.tasks.conversations(owner=owner(request))

    @router.get("/conversations/{conversation_id}")
    def conversation(conversation_id: str, request: Request):
        return runtime.tasks.conversation(conversation_id, owner=owner(request))

    @router.get("/conversations/{conversation_id}/export")
    def export_conversation(conversation_id: str, request: Request):
        """Every turn's report and evidence, independently of the turns that came after.

        Reports and evidence only: no messages, no checkpoint, no model configuration. A later turn
        is a new version, so exporting the conversation cannot overwrite an earlier conclusion.
        """
        identity = owner(request)
        conversation = runtime.tasks.conversation(conversation_id, owner=identity)
        turns = []
        for run in conversation["runs"]:
            stored = runtime.tasks.get(run["id"], owner=identity)
            turns.append({"run_id": run["id"], "turn": run["turn"], "kind": run["kind"],
                          "status": run["status"], "goal": run["goal"], "created_at": run["created_at"],
                          "report": stored.get("report"), "report_delta": stored.get("report_delta"),
                          "origin": stored.get("origin"), "usage": stored.get("usage"),
                          "evidence": [{k: v for k, v in item.items() if k != "content"}
                                       for item in stored.get("evidence", [])]})
        return JSONResponse({**{k: v for k, v in conversation.items() if k != "runs"}, "turns": turns,
                             "note": "历史报告与证据独立导出；没有包含对话消息、检查点或模型配置"},
                            headers={"Content-Disposition":
                                     'attachment; filename="re0-research-conversation.json"'})

    @router.post("/followups/scope")
    def followup_scope(data: FollowUpInput, request: Request):
        """What a follow-up would be allowed to touch, without creating it. Spends nothing."""
        return runtime.scope_preview(data, owner=owner(request))

    @router.post("/followups", status_code=202)
    def followup(data: FollowUpInput, request: Request):
        return runtime.follow_up(data, owner=owner(request))

    @router.post("/retries", status_code=202)
    def retry(data: RetryInput, request: Request):
        return runtime.retry(data, owner=owner(request))

    @router.get("/runs/{run_id}/origin")
    def origin(run_id: str, request: Request):
        """The immutable snapshot of what authorized this turn: goal, destination, permissions, budget."""
        return runtime.tasks.origin(run_id, owner=owner(request))

    @router.get("/runs/{run_id}/delta")
    def delta(run_id: str, request: Request):
        stored = runtime.tasks.get(run_id, owner=owner(request))
        return stored.get("report_delta") or {"note": "这一轮没有已完成的报告，或它是会话的第一轮"}

    @router.post("/runs/{run_id}/evidence/{evidence_id}/import")
    def approve(run_id: str, evidence_id: str, data: Approval, request: Request):
        # The approval writes into the approver's own library, and only from evidence their own task
        # retrieved: there is no argument that turns somebody else's run into this caller's paper.
        return runtime.tasks.import_paper(run_id, evidence_id, owner=owner(request))

    @router.get("/runs/{run_id}/export")
    def export(run_id: str, request: Request):
        # No raw prompts/checkpoint/messages or credentials in the export.
        return JSONResponse(runtime.tasks.get(run_id, owner=owner(request)),
                            headers={"Content-Disposition": 'attachment; filename="re0-research-task.json"'})

    return router
