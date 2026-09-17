"""Task APIs expose progress and source evidence, never model credentials/checkpoints."""
from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse

from .model import ModelError
from .schemas import Approval, ModelConfig, ModelListRequest, TaskDefaults, TaskInput


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

    @router.post("/runs/{run_id}/evidence/{evidence_id}/import")
    def approve(run_id: str, evidence_id: str, data: Approval):
        return runtime.tasks.import_paper(run_id, evidence_id)

    @router.get("/runs/{run_id}/export")
    def export(run_id: str):
        # No raw prompts/checkpoint/messages or credentials in the export.
        return JSONResponse(runtime.tasks.get(run_id), headers={"Content-Disposition": 'attachment; filename="re0-research-task.json"'})

    return router
