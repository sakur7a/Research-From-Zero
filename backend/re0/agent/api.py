"""Task APIs expose progress and source evidence, never model credentials/checkpoints."""
from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse

from .model import ModelError
from .schemas import (Approval, FollowUpInput, ModelConfig, ModelListRequest, RetryInput, SessionCaps,
                      TaskDefaults, TaskInput)


def agent_router(runtime):
    router = APIRouter(prefix="/api/agent", tags=["Research agent"])

    @router.get("/config")
    def config():
        return runtime.public()

    @router.put("/config")
    def configure(data: ModelConfig):
        return runtime.configure(data)

    @router.delete("/config")
    def clear():
        return runtime.configure(None)

    @router.post("/config/test")
    def test():
        return runtime.test_connection()

    @router.post("/models")
    def models(data: ModelListRequest):
        # Read-only probe used by the settings picker. The key is not persisted.
        return runtime.list_models(data)

    @router.put("/defaults")
    def defaults(data: TaskDefaults):
        # Workspace defaults for new tasks; not credentials, so they are readable again.
        return runtime.set_defaults(data)

    @router.put("/session-defaults")
    def session_defaults(data: SessionCaps):
        """Ceilings for conversations created from now on. Existing conversations keep their own."""
        return runtime.set_session_caps(data)

    @router.get("/runs")
    def tasks():
        return runtime.tasks.list()

    @router.post("/runs", status_code=202)
    def start(data: TaskInput):
        return runtime.submit(data)

    @router.get("/runs/{run_id}")
    def task(run_id: str):
        return runtime.tasks.get(run_id)

    @router.get("/runs/{run_id}/events")
    def events(run_id: str, after: int = Query(0, ge=0)):
        return runtime.tasks.events(run_id, after)

    @router.post("/runs/{run_id}/cancel")
    def cancel(run_id: str):
        runtime.tasks.request_cancel(run_id)
        return {"requested": True, "note": "在当前调用结束/超时后的下一安全边界停止"}

    @router.post("/runs/{run_id}/resume", status_code=202)
    def resume(run_id: str, data: Approval):
        return runtime.resume(run_id)

    @router.get("/conversations")
    def conversations():
        # A conversation is the line of turns that shares evidence and one cumulative ledger.
        return runtime.tasks.conversations()

    @router.get("/conversations/{conversation_id}")
    def conversation(conversation_id: str):
        return runtime.tasks.conversation(conversation_id)

    @router.get("/conversations/{conversation_id}/export")
    def export_conversation(conversation_id: str):
        """Every turn's report and evidence, independently of the turns that came after.

        Reports and evidence only: no messages, no checkpoint, no model configuration. A later turn
        is a new version, so exporting the conversation cannot overwrite an earlier conclusion.
        """
        conversation = runtime.tasks.conversation(conversation_id)
        turns = []
        for run in conversation["runs"]:
            stored = runtime.tasks.get(run["id"])
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
    def followup_scope(data: FollowUpInput):
        """What a follow-up would be allowed to touch, without creating it. Spends nothing."""
        return runtime.scope_preview(data)

    @router.post("/followups", status_code=202)
    def followup(data: FollowUpInput):
        return runtime.follow_up(data)

    @router.post("/retries", status_code=202)
    def retry(data: RetryInput):
        return runtime.retry(data)

    @router.get("/runs/{run_id}/origin")
    def origin(run_id: str):
        """The immutable snapshot of what authorized this turn: goal, destination, permissions, budget."""
        return runtime.tasks.origin(run_id)

    @router.get("/runs/{run_id}/delta")
    def delta(run_id: str):
        stored = runtime.tasks.get(run_id)
        return stored.get("report_delta") or {"note": "这一轮没有已完成的报告，或它是会话的第一轮"}

    @router.post("/runs/{run_id}/evidence/{evidence_id}/import")
    def approve(run_id: str, evidence_id: str, data: Approval):
        return runtime.tasks.import_paper(run_id, evidence_id)

    @router.get("/runs/{run_id}/export")
    def export(run_id: str):
        # No raw prompts/checkpoint/messages or credentials in the export.
        return JSONResponse(runtime.tasks.get(run_id), headers={"Content-Disposition": 'attachment; filename="re0-research-task.json"'})

    return router
