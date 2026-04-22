from __future__ import annotations
from prefect import flow  # noqa: F401 - Workaround para bug circular de Prefect 3, debe ir primero

"""
FastAPI orchestrator. Dispara flow runs de Prefect sin crear el objeto State
manualmente (que es lo que rompía con el bug de BaseResult).
"""

import html
import logging
import os
import re
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import httpx
import markdown as md
from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from orchestrator.config import OLLAMA_HOST_FROM_CONTAINER
from prefect.client.orchestration import get_client
from prefect.client.schemas.objects import StateType
from prefect.deployments import run_deployment
from prefect.states import Cancelled
from pydantic import BaseModel, Field

from orchestrator import logger as tasklog
from orchestrator import preview as preview_state

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("pipeline")

app = FastAPI(title="pipeline-ia (Prefect)")

templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

REPOS_ROOT = Path("/repos")
DEPLOYMENT_NAME = os.environ.get("PIPELINE_DEPLOYMENT_NAME", "pipeline/default")
TEARDOWN_DEPLOYMENT_NAME = os.environ.get(
    "PIPELINE_TEARDOWN_DEPLOYMENT_NAME", "teardown-preview/default"
)
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


# --- UI helpers: selectable branches + models ---

def _list_git_branches(repo_path: str) -> list[str]:
    """Local branches, read straight from .git/ — no git binary required."""
    try:
        resolved = _resolve_repo_path(repo_path)
    except HTTPException:
        return []
    git_dir = resolved / ".git"
    if not git_dir.is_dir():
        return []

    branches: set[str] = set()

    heads = git_dir / "refs" / "heads"
    if heads.is_dir():
        for path in heads.rglob("*"):
            if path.is_file():
                branches.add(str(path.relative_to(heads)).replace("\\", "/"))

    packed = git_dir / "packed-refs"
    if packed.is_file():
        try:
            for line in packed.read_text(encoding="utf-8", errors="replace").splitlines():
                line = line.strip()
                if not line or line.startswith(("#", "^")):
                    continue
                parts = line.split(" ", 1)
                if len(parts) == 2 and parts[1].startswith("refs/heads/"):
                    branches.add(parts[1][len("refs/heads/"):])
        except OSError:
            pass

    return sorted(branches)


def _pick_default(options: list[str], preferred: list[str]) -> str | None:
    for p in preferred:
        if p in options:
            return p
    return options[0] if options else None


def _options_html(items: list[str], selected: str | None, empty_label: str) -> str:
    if not items:
        return f'<option value="" disabled selected>{html.escape(empty_label)}</option>'
    out = []
    for item in items:
        mark = " selected" if item == selected else ""
        safe = html.escape(item)
        out.append(f'<option value="{safe}"{mark}>{safe}</option>')
    return "\n".join(out)


@app.get("/ui/branches", response_class=HTMLResponse)
async def ui_branches(repo_path: str = "target-repo", selected: str | None = None) -> str:
    branches = _list_git_branches(repo_path)
    default = selected if selected in branches else _pick_default(branches, ["main", "master"])
    return _options_html(branches, default, empty_label="(repo no disponible)")


_REPO_SCAN_MAX_DEPTH = 4


def _list_git_repos() -> list[str]:
    """Git repos bajo /repos (hasta 4 niveles). Paths relativos a /repos."""
    if not REPOS_ROOT.is_dir():
        return []
    found: list[str] = []

    def walk(path: Path, depth: int) -> None:
        if depth > _REPO_SCAN_MAX_DEPTH:
            return
        try:
            entries = list(path.iterdir())
        except OSError:
            return
        for entry in entries:
            if not entry.is_dir() or entry.name.startswith("."):
                continue
            if (entry / ".git").exists():
                found.append(str(entry.relative_to(REPOS_ROOT)))
                # No descendemos dentro de un repo encontrado.
                continue
            walk(entry, depth + 1)

    walk(REPOS_ROOT, 0)
    return sorted(found)


@app.get("/ui/repos", response_class=HTMLResponse)
async def ui_repos(selected: str | None = None) -> str:
    repos = _list_git_repos()
    default = selected if selected in repos else _pick_default(repos, ["target-repo"])
    return _options_html(repos, default, empty_label="(sin repos en /repos)")


async def _list_ollama_models() -> list[str]:
    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            r = await client.get(f"{OLLAMA_HOST_FROM_CONTAINER}/api/tags")
            r.raise_for_status()
            data = r.json()
    except (httpx.HTTPError, ValueError):
        return []
    names = [m.get("name") for m in data.get("models", []) if m.get("name")]
    return sorted(names)


@app.get("/ui/models", response_class=HTMLResponse)
async def ui_models(selected: str | None = None) -> str:
    models = await _list_ollama_models()
    default = selected if selected in models else _pick_default(models, [DEFAULT_MODEL])
    # When Ollama is unreachable, still let the user submit the default model.
    if not models:
        return f'<option value="{html.escape(DEFAULT_MODEL)}" selected>{html.escape(DEFAULT_MODEL)} (ollama offline)</option>'
    return _options_html(models, default, empty_label="(sin modelos)")


# --- preview endpoints ---

async def _task_id_from_flow_run(flow_run_id: str) -> int:
    try:
        run_uuid = uuid.UUID(flow_run_id)
    except ValueError:
        raise HTTPException(400, "flow_run_id must be a UUID")
    async with get_client() as client:
        try:
            run = await client.read_flow_run(run_uuid)
        except Exception:
            raise HTTPException(404, "Flow run not found")
    raw = (run.parameters or {}).get("task_id")
    if raw is None:
        raise HTTPException(404, "task_id not present in flow run parameters")
    return int(raw)


@app.get("/tasks/{flow_run_id}/preview")
async def get_preview(flow_run_id: str) -> dict:
    task_id = await _task_id_from_flow_run(flow_run_id)
    state = preview_state.load_sidecar(task_id)
    if state is None:
        raise HTTPException(404, "No active preview for this task")
    return {
        "task_id": task_id,
        "url": state["url"],
        "port": state["port"],
    }


@app.delete("/tasks/{flow_run_id}/preview")
async def delete_preview(flow_run_id: str) -> dict:
    """Dispara el teardown en el worker (que tiene docker.sock + worktrees).
    Bloqueante: espera a que termine."""
    task_id = await _task_id_from_flow_run(flow_run_id)
    state = preview_state.load_sidecar(task_id)
    if state is None:
        raise HTTPException(404, "No active preview for this task")

    teardown_run = await run_deployment(
        name=TEARDOWN_DEPLOYMENT_NAME,
        parameters={"task_id": task_id},
        timeout=300,
        tags=[f"teardown-preview-{task_id}"],
    )
    return {
        "task_id": task_id,
        "teardown_run_id": str(teardown_run.id),
        "state": teardown_run.state_name if teardown_run.state else "UNKNOWN",
    }


# --- HTML dashboard (opción A) ---

def _task_row(run_dict: dict) -> dict:
    """Aplana un flow_run dict para los templates."""
    params = run_dict.get("parameters") or {}
    task_id = params.get("task_id")
    preview = preview_state.load_sidecar(int(task_id)) if task_id is not None else None
    return {
        "id": run_dict["id"],
        "state": run_dict.get("state") or "UNKNOWN",
        "created": run_dict.get("created"),
        "task_id": task_id,
        "prompt": (params.get("prompt") or "")[:200],
        "preview_url": preview["url"] if preview else None,
    }


def _active_previews() -> list[dict]:
    """Lista previews activas leyendo los sidecars. Enriquece con flow_run_id
    si encuentra el flow run con matching task_id."""
    out = []
    logs_dir = Path(os.environ.get("PIPELINE_LOGS", "/app/logs"))
    for sidecar in sorted(logs_dir.glob("task-*.preview.json")):
        try:
            import json
            data = json.loads(sidecar.read_text(encoding="utf-8"))
            out.append({
                "task_id": data.get("task_id"),
                "url": data.get("url"),
                "port": data.get("port"),
                "flow_run_id": None,  # resuelto en dashboard() si se puede
            })
        except Exception:
            continue
    return out


@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    raw_tasks = await list_tasks(limit=50)
    tasks = [_task_row(t) for t in raw_tasks]

    # Cruzar previews con flow runs por task_id para que el botón de teardown
    # tenga un flow_run_id válido.
    by_task_id = {t["task_id"]: t["id"] for t in tasks if t.get("task_id") is not None}
    previews = _active_previews()
    for p in previews:
        if p["task_id"] in by_task_id:
            p["flow_run_id"] = by_task_id[p["task_id"]]

    return templates.TemplateResponse("index.html", {
        "request": request,
        "tasks": tasks,
        "previews": previews,
        "default_model": DEFAULT_MODEL,
    })


@app.get("/ui/tasks", response_class=HTMLResponse)
async def dashboard_tasks_partial(request: Request):
    """Partial para el polling htmx."""
    raw_tasks = await list_tasks(limit=50)
    tasks = [_task_row(t) for t in raw_tasks]
    return templates.TemplateResponse("_tasks_list.html", {
        "request": request, "tasks": tasks,
    })


@app.post("/ui/tasks")
async def dashboard_create(
    prompt: str = Form(...),
    repo_path: str = Form("target-repo"),
    base_branch: str = Form("main"),
    model: str = Form(DEFAULT_MODEL),
):
    resp = await create_task(TaskCreate(
        prompt=prompt, repo_path=repo_path, base_branch=base_branch, model=model,
    ))
    return RedirectResponse(url=f"/ui/tasks/{resp.id}", status_code=303)


_LOG_ISO_RE = re.compile(r"(\d{4}-\d{2}-\d{2})T(\d{2}:\d{2}:\d{2})(?:\.\d+)?Z?")


def _fmt_iso_utc(iso_str: str | None) -> str | None:
    """Render an ISO8601 timestamp as 'YYYY-MM-DD HH:MM:SS UTC'."""
    if not iso_str:
        return None
    try:
        dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
    except ValueError:
        return iso_str
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc)
    return dt.strftime("%Y-%m-%d %H:%M:%S UTC")


def _prettify_log_timestamps(md_source: str) -> str:
    """Rewrite verbose ISO timestamps inside log markdown to a readable form."""
    return _LOG_ISO_RE.sub(r"\1 \2 UTC", md_source)


@app.get("/ui/tasks/{flow_run_id}", response_class=HTMLResponse)
async def dashboard_task_detail(request: Request, flow_run_id: str):
    run = await get_task(flow_run_id)
    params = run.get("parameters") or {}
    task_id = params.get("task_id")

    log_html = ""
    if task_id is not None:
        log_path = tasklog.log_path(int(task_id))
        if log_path.exists():
            log_html = md.markdown(
                _prettify_log_timestamps(log_path.read_text(encoding="utf-8")),
                extensions=["fenced_code", "tables"],
            )

    preview = None
    if task_id is not None:
        preview = preview_state.load_sidecar(int(task_id))

    task_ctx = {
        "id": run["id"],
        "state": run["state"],
        "task_id": task_id,
        "prompt": params.get("prompt", ""),
        "feature_branch": params.get("feature_branch") or f"pipeline/task-{task_id}",
        "model": params.get("model"),
        "ui_url": run.get("ui_url"),
        "created": _fmt_iso_utc(run.get("start_time")),
    }
    return templates.TemplateResponse("task_detail.html", {
        "request": request,
        "task": task_ctx,
        "log_html": log_html,
        "preview": preview,
    })


@app.post("/ui/tasks/{flow_run_id}/cancel")
async def dashboard_cancel(flow_run_id: str):
    try:
        run_uuid = uuid.UUID(flow_run_id)
    except ValueError:
        raise HTTPException(400, "flow_run_id must be a UUID")
    async with get_client() as client:
        await client.set_flow_run_state(run_uuid, Cancelled(), force=True)
    return RedirectResponse(url=f"/ui/tasks/{flow_run_id}", status_code=303)


@app.post("/ui/tasks/{flow_run_id}/preview/teardown")
async def dashboard_teardown(flow_run_id: str):
    await delete_preview(flow_run_id)
    return RedirectResponse(url=f"/ui/tasks/{flow_run_id}", status_code=303)