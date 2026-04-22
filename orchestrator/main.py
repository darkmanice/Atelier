from __future__ import annotations
from prefect import flow  # noqa: F401 - Workaround para bug circular de Prefect 3, debe ir primero

"""
FastAPI orchestrator. Dispara flow runs de Prefect sin crear el objeto State
manualmente (que es lo que rompía con el bug de BaseResult).
"""

import logging
import os
import time
import uuid
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import PlainTextResponse
from prefect.client.orchestration import get_client
from prefect.deployments import run_deployment
from pydantic import BaseModel, Field

from orchestrator import logger as tasklog

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("pipeline")

app = FastAPI(title="pipeline-ia (Prefect)")

REPOS_ROOT = Path("/repos")
DEPLOYMENT_NAME = os.environ.get("PIPELINE_DEPLOYMENT_NAME", "pipeline/default")
DEFAULT_MODEL = os.environ.get("PIPELINE_MODEL", "gemma4:26b")


# --- schemas ---

class TaskCreate(BaseModel):
    prompt: str = Field(min_length=5)
    repo_path: str = Field(description="Ruta relativa a /repos o absoluta.")
    base_branch: str = "main"
    feature_branch: str | None = None
    model: str = DEFAULT_MODEL


class TaskResponse(BaseModel):
    id: str
    task_id: int
    state: str
    prefect_ui_url: str


def _resolve_repo_path(user_input: str) -> Path:
    p = Path(user_input)
    if not p.is_absolute():
        p = REPOS_ROOT / p
    p = p.resolve()
    if not str(p).startswith(str(REPOS_ROOT)):
        raise HTTPException(400, f"repo_path must be under {REPOS_ROOT}")
    if not (p / ".git").exists():
        raise HTTPException(400, f"{p} is not a git repository")
    return p


# --- endpoints ---

@app.get("/health")
async def health() -> dict:
    try:
        async with get_client() as client:
            await client.hello()
        return {"status": "ok", "prefect": "connected"}
    except Exception as e:
        return {"status": "degraded", "prefect": f"unreachable: {e}"}


@app.post("/tasks", response_model=TaskResponse, status_code=201)
async def create_task(payload: TaskCreate) -> TaskResponse:
    repo_path = _resolve_repo_path(payload.repo_path)

    task_id = int(time.time() * 1000) % 1_000_000

    parameters = {
        "task_id": task_id,
        "prompt": payload.prompt,
        "repo_path": str(repo_path),
        "base_branch": payload.base_branch,
        "feature_branch": payload.feature_branch,
        "model": payload.model,
    }

    # run_deployment evita tocar State manualmente (Prefect lo hace internamente).
    # timeout=0 → fire-and-forget: devuelve inmediatamente sin esperar resultado.
    flow_run = await run_deployment(
        name=DEPLOYMENT_NAME,
        parameters=parameters,
        timeout=0,
        tags=[f"task-{task_id}"],
    )

    ui_url = f"http://localhost:4200/runs/flow-run/{flow_run.id}"
    log.info(f"Created flow run {flow_run.id} (task_id={task_id}) → {ui_url}")

    return TaskResponse(
        id=str(flow_run.id),
        task_id=task_id,
        state=flow_run.state_name if flow_run.state else "SCHEDULED",
        prefect_ui_url=ui_url,
    )


@app.get("/tasks")
async def list_tasks(limit: int = 50) -> list[dict]:
    async with get_client() as client:
        deployment = await client.read_deployment_by_name(DEPLOYMENT_NAME)
        from prefect.client.schemas.filters import FlowRunFilter, FlowRunFilterDeploymentId
        runs = await client.read_flow_runs(
            flow_run_filter=FlowRunFilter(
                deployment_id=FlowRunFilterDeploymentId(any_=[deployment.id])
            ),
            limit=limit,
        )
    return [
        {
            "id": str(r.id),
            "state": r.state_name if r.state else "UNKNOWN",
            "name": r.name,
            "created": r.created.isoformat() if r.created else None,
            "tags": list(r.tags or []),
            "parameters": dict(r.parameters or {}),
        }
        for r in runs
    ]


@app.get("/tasks/{flow_run_id}")
async def get_task(flow_run_id: str) -> dict:
    async with get_client() as client:
        try:
            run = await client.read_flow_run(uuid.UUID(flow_run_id))
        except Exception:
            raise HTTPException(404, "Flow run not found")
    return {
        "id": str(run.id),
        "state": run.state_name if run.state else "UNKNOWN",
        "name": run.name,
        "parameters": dict(run.parameters or {}),
        "start_time": run.start_time.isoformat() if run.start_time else None,
        "end_time": run.end_time.isoformat() if run.end_time else None,
        "ui_url": f"http://localhost:4200/runs/flow-run/{run.id}",
    }


@app.get("/tasks/{task_id}/log", response_class=PlainTextResponse)
async def get_task_log(task_id: int) -> str:
    path = tasklog.log_path(task_id)
    if not path.exists():
        raise HTTPException(404, f"Log for task {task_id} not found")
    return path.read_text(encoding="utf-8")