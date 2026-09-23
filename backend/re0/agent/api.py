"""Task APIs expose progress and source evidence, never model credentials/checkpoints.

Every route here takes the request only to read one thing from it: the identity the middleware
verified. The owner is never a parameter a caller can send, because a parameter is something a caller
can also change.
"""
from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import JSONResponse, Response
from pydantic import Field, model_validator

from .. import resource_matrix
from .matrix import build_run_matrix, selected_import_items
from .model import ModelError
from .schemas import (Approval, FollowUpInput, ModelConfig, ModelListRequest, RetryInput, SessionCaps,
                      StrictModel, TaskDefaults, TaskInput)


class MatrixSelection(StrictModel):
    paper_evidence_id: str = Field(min_length=1, max_length=80)
    resource_evidence_id: str = Field(min_length=1, max_length=80)


class MatrixImportRequest(StrictModel):
    selections: list[MatrixSelection] = Field(min_length=1, max_length=40)

    @model_validator(mode="after")
    def unique_selections(self):
        pairs = [(item.paper_evidence_id, item.resource_evidence_id) for item in self.selections]
        if len(pairs) != len(set(pairs)):
            raise ValueError("同一候选关联不能重复选择")
        return self


def agent_router(runtime, library_store=None):
    router = APIRouter(prefix="/api/agent", tags=["Research agent"])

    def owner(request: Request) -> str:
        return request.state.identity.owner

    @router.get("/config")
    def config(request: Request):
        return runtime.public(owner=owner(request))

    @router.put("/config")
    def configure(data: ModelConfig, request: Request):
        return runtime.configure(data, owner=owner(request))

    @router.post("/config/connect")
    def connect(data: ModelConfig, request: Request):
        # The key is tested before it is retained, then remains only in the server's memory vault.
        return runtime.connect(data, owner=owner(request))

    @router.delete("/config")
    def clear(request: Request):
        return runtime.configure(None, owner=owner(request))

    @router.post("/config/test")
    def test(request: Request):
        return runtime.test_connection(owner=owner(request))

    @router.post("/models")
    def models(data: ModelListRequest, request: Request):
        # Read-only probe used by the settings picker. The key is not persisted, and the probe is
        # scoped to the credentials in this request rather than to any stored configuration.
        return runtime.list_models(data, owner=owner(request))

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

    @router.get("/runs/{run_id}/progress")
    def progress(run_id: str, request: Request,
                 after: int = Query(0, ge=0), evidence_after: int = Query(0, ge=0)):
        return runtime.tasks.progress(run_id, after, evidence_after, owner=owner(request))

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

    def matrix_for_run(run_id: str, request: Request) -> dict:
        run = runtime.tasks.get(run_id, owner=owner(request))
        if not run.get("report"):
            raise HTTPException(409, "任务尚未提交结构化报告；完成后才能生成资源矩阵")
        return build_run_matrix(run)

    @router.get("/runs/{run_id}/matrix")
    def matrix_export(run_id: str, request: Request,
                      format: str = Query("json", pattern="^(json|csv|markdown)$")):
        payload = matrix_for_run(run_id, request)
        if format == "csv":
            return Response(resource_matrix.csv_text(payload), media_type="text/csv; charset=utf-8",
                            headers={"Content-Disposition": 'attachment; filename="re0-resource-matrix.csv"'})
        if format == "markdown":
            return Response(resource_matrix.markdown(payload), media_type="text/markdown; charset=utf-8",
                            headers={"Content-Disposition": 'attachment; filename="re0-resource-matrix.md"'})
        return JSONResponse(payload, headers={"Content-Disposition":
                                              'attachment; filename="re0-resource-matrix.json"'})

    @router.post("/runs/{run_id}/matrix/preview")
    def matrix_import_preview(run_id: str, data: MatrixImportRequest, request: Request):
        if library_store is None:
            raise HTTPException(503, "文献库不可用")
        payload = matrix_for_run(run_id, request)
        items = selected_import_items(payload, [item.model_dump() for item in data.selections])
        return library_store.import_audits(items, dry_run=True, owner=owner(request))

    @router.post("/runs/{run_id}/matrix/confirm")
    def matrix_import_confirm(run_id: str, data: MatrixImportRequest, request: Request):
        if library_store is None:
            raise HTTPException(503, "文献库不可用")
        payload = matrix_for_run(run_id, request)
        items = selected_import_items(payload, [item.model_dump() for item in data.selections])
        return library_store.import_audits(items, dry_run=False, owner=owner(request))

    return router
