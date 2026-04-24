from __future__ import annotations
from prefect import flow  # noqa: F401 - Workaround for Prefect 3 circular-import bug, must go first

"""
FastAPI orchestrator. Fires Prefect flow runs without creating the State
object manually (which is what broke with the BaseResult bug).
"""

import asyncio
import html
import logging
import os
import re
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import httpx
import markdown as md
from fastapi import FastAPI, Form, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from prefect.client.orchestration import get_client
from prefect.client.schemas.objects import StateType
from prefect.deployments import run_deployment
from prefect.states import Cancelled
from pydantic import BaseModel, Field, SecretStr

from orchestrator import logger as tasklog
from orchestrator import preview as preview_state
from orchestrator.config import DEFAULT_MODEL, PROJECTS_ROOT
from orchestrator.crypto import CryptoError, is_available as crypto_available
from orchestrator.providers_store import store as providers_store
from orchestrator.secrets_store import store as secret_store

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("pipeline")

app = FastAPI(title="atelier")

templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

DEPLOYMENT_NAME = os.environ.get("DEPLOYMENT_NAME", "atelier/default")
TEARDOWN_DEPLOYMENT_NAME = os.environ.get(
    "TEARDOWN_DEPLOYMENT_NAME", "teardown-preview/default"
)
# Shared secret between orchestrator and worker for the /internal/* endpoints.
# MUST match in both services (docker-compose.yml -> env from .env).
INTERNAL_TOKEN = os.environ.get("INTERNAL_API_TOKEN", "").strip()
if not INTERNAL_TOKEN:
    # We do not abort so we do not break dev when there are no flows in flight,
    # but the /internal/* endpoints will reject everything while it is empty.
    logging.getLogger("pipeline").warning(
        "INTERNAL_API_TOKEN is unset; /internal/* endpoints will reject all requests."
    )


# --- schemas ---

class TaskCreate(BaseModel):
    prompt: str = Field(min_length=5)
    repo_path: str = Field(description="Path relative to /projects or absolute.")
    base_branch: str = "main"
    feature_branch: str | None = None

    # --- LLM config ---
    # There are two mutually exclusive modes:
    # 1. Saved provider: send only `provider_id` (optionally `model`
    #    to override the provider's default).
    # 2. One-shot: send `base_url` + `api_key` (+ cosmetic `provider_label`).
    provider_id: str | None = None
    provider_label: str = Field(default="custom", description="Cosmetic label (one-shot mode).")
    base_url: str | None = None
    model: str | None = None
    # SecretStr prevents an accidental repr/dump from printing the key in logs.
    api_key: SecretStr | None = None

    # Per-role overrides only for THIS task. Priority:
    #   per-role task > global task > per-role provider > provider default.
    model_implementer: str | None = None
    model_reviewer: str | None = None
    model_simplifier: str | None = None


class ProviderCreate(BaseModel):
    label: str = Field(min_length=1, max_length=80)
    provider_label: str = "custom"
    base_url: str
    model: str
    api_key: SecretStr
    # Per-role overrides. Empty = uses `model`.
    model_implementer: str = ""
    model_reviewer: str = ""
    model_simplifier: str = ""


class TaskResponse(BaseModel):
    id: str
    task_id: int
    state: str
    prefect_ui_url: str


def _resolve_repo_path(user_input: str) -> Path:
    p = Path(user_input)
    if not p.is_absolute():
        p = PROJECTS_ROOT / p
    p = p.resolve()
    if not str(p).startswith(str(PROJECTS_ROOT)):
        raise HTTPException(400, f"repo_path must be under {PROJECTS_ROOT}")
    if not (p / ".git").exists():
        raise HTTPException(400, f"{p} is not a git repository")
    return p


# --- endpoints ---

@app.on_event("startup")
async def _start_secret_reaper() -> None:
    """GC of expired entries in the secret store. Cheap, only every 60s."""
    async def loop() -> None:
        while True:
            await asyncio.sleep(60)
            try:
                reaped = secret_store.reap()
                if reaped:
                    log.debug("Reaped %d expired secret(s)", reaped)
            except Exception:
                log.exception("Secret reaper failed")
    asyncio.create_task(loop())


@app.get("/health")
async def health() -> dict:
    try:
        async with get_client() as client:
            await client.hello()
        return {"status": "ok", "prefect": "connected"}
    except Exception as e:
        return {"status": "degraded", "prefect": f"unreachable: {e}"}


# --- internal endpoints (worker → orchestrator) ---

class ConsumeSecretRequest(BaseModel):
    token: str


def _require_internal_auth(x_internal_token: str | None) -> None:
    if not INTERNAL_TOKEN:
        raise HTTPException(503, "Internal auth not configured")
    if not x_internal_token or not secret_compare(x_internal_token, INTERNAL_TOKEN):
        raise HTTPException(401, "Invalid internal token")


def secret_compare(a: str, b: str) -> bool:
    """Constant-time comparison. Avoids timing oracles on the token."""
    import hmac
    return hmac.compare_digest(a.encode("utf-8"), b.encode("utf-8"))


@app.post("/internal/consume-secret")
async def consume_secret(
    body: ConsumeSecretRequest,
    x_internal_token: str | None = Header(default=None, alias="X-Internal-Token"),
) -> dict:
    """
    One-shot handoff worker <- orchestrator. Deletes the entry.

    We do not log the token or the result so we leave no trace in stdout.
    """
    _require_internal_auth(x_internal_token)
    api_key = secret_store.consume(body.token)
    if api_key is None:
        raise HTTPException(404, "Secret not found or expired")
    return {"api_key": api_key}


@dataclass
class _ResolvedLLM:
    provider_label: str
    base_url: str
    model_implementer: str
    model_reviewer: str
    model_simplifier: str
    api_key: str | None


def _resolve_llm_from_payload(payload: TaskCreate) -> _ResolvedLLM:
    """
    Flattens the two forms (provider_id or one-shot) into a config with a
    per-role model. Priority: per-role task override > global task override >
    per-role provider override > provider default model.
    """
    global_override = (payload.model or "").strip() or None

    def _role_task_override(role: str) -> str | None:
        v = getattr(payload, f"model_{role}", None)
        return (v or "").strip() or None if v is not None else None

    if payload.provider_id:
        resolved = providers_store.get_decrypted_key(payload.provider_id)
        if resolved is None:
            raise HTTPException(404, f"Provider {payload.provider_id} not found")
        prov, api_key = resolved

        def pick(role: str) -> str:
            return (
                _role_task_override(role)
                or global_override
                or prov.model_for_role(role)
            )

        chosen = {
            "implementer": pick("implementer"),
            "reviewer": pick("reviewer"),
            "simplifier": pick("simplifier"),
        }
        missing = [r for r, m in chosen.items() if not m]
        if missing:
            raise HTTPException(
                400,
                f"Provider has no model configured for role(s): {', '.join(missing)}",
            )

        return _ResolvedLLM(
            provider_label=prov.provider_label,
            base_url=prov.base_url,
            model_implementer=chosen["implementer"],
            model_reviewer=chosen["reviewer"],
            model_simplifier=chosen["simplifier"],
            api_key=api_key,
        )

    # One-shot mode: base_url is required; per-role model accepts overrides.
    if not payload.base_url:
        raise HTTPException(400, "base_url is required without a provider_id")
    if not global_override and not all(_role_task_override(r) for r in ("implementer", "reviewer", "simplifier")):
        raise HTTPException(400, "model (or all three per-role models) is required without a provider_id")
    api_key = payload.api_key.get_secret_value() if payload.api_key else None

    def pick_oneshot(role: str) -> str:
        return _role_task_override(role) or global_override or ""

    return _ResolvedLLM(
        provider_label=payload.provider_label,
        base_url=payload.base_url,
        model_implementer=pick_oneshot("implementer"),
        model_reviewer=pick_oneshot("reviewer"),
        model_simplifier=pick_oneshot("simplifier"),
        api_key=api_key,
    )


@app.post("/tasks", response_model=TaskResponse, status_code=201)
async def create_task(payload: TaskCreate) -> TaskResponse:
    repo_path = _resolve_repo_path(payload.repo_path)

    task_id = int(time.time() * 1000) % 1_000_000

    try:
        resolved = _resolve_llm_from_payload(payload)
    except CryptoError as e:
        # Decryption failed: rotated master key, or changed .env, or corrupted disk.
        raise HTTPException(500, f"Cannot decrypt provider secret: {e}") from e

    # Store the api_key in RAM and pass only an opaque token to the flow. This
    # way it does not enter the flow run parameters (visible in the Prefect UI).
    # If there is no key (endpoint without auth), there is no token.
    secret_token: str | None = None
    if resolved.api_key:
        secret_token = secret_store.stash(resolved.api_key)

    parameters = {
        "task_id": task_id,
        "prompt": payload.prompt,
        "repo_path": str(repo_path),
        "base_branch": payload.base_branch,
        "feature_branch": payload.feature_branch,
        "provider_label": resolved.provider_label,
        "base_url": resolved.base_url,
        "model_implementer": resolved.model_implementer,
        "model_reviewer": resolved.model_reviewer,
        "model_simplifier": resolved.model_simplifier,
        "secret_token": secret_token,
    }

    # run_deployment avoids touching State manually (Prefect does it internally).
    # timeout=0 -> fire-and-forget: returns immediately without waiting for the result.
    try:
        flow_run = await run_deployment(
            name=DEPLOYMENT_NAME,
            parameters=parameters,
            timeout=0,
            tags=[f"task-{task_id}"],
        )
    except Exception:
        # If the deployment could not be created, the key would be orphaned in
        # the store until the TTL. Better to delete it now.
        if secret_token is not None:
            secret_store.discard(secret_token)
        raise

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
async def ui_branches(repo_path: str = "", selected: str | None = None) -> str:
    branches = _list_git_branches(repo_path)
    default = selected if selected in branches else _pick_default(branches, ["main", "master"])
    return _options_html(branches, default, empty_label="(repo not available)")


def _list_git_repos() -> list[str]:
    """Git repos under /projects (recursive, no limit). Paths relative to /projects."""
    if not PROJECTS_ROOT.is_dir():
        return []
    found: list[str] = []

    def walk(path: Path) -> None:
        try:
            entries = list(path.iterdir())
        except OSError:
            return
        for entry in entries:
            if not entry.is_dir() or entry.name.startswith("."):
                continue
            if (entry / ".git").exists():
                found.append(str(entry.relative_to(PROJECTS_ROOT)))
                # We do not descend inside a found repo.
                continue
            walk(entry)

    walk(PROJECTS_ROOT)
    return sorted(found)


@app.get("/ui/projects", response_class=HTMLResponse)
async def ui_repos(selected: str | None = None) -> str:
    repos = _list_git_repos()
    default = selected if selected in repos else _pick_default(repos, [])
    return _options_html(repos, default, empty_label="(no repos in /projects)")


class ModelsProbeRequest(BaseModel):
    base_url: str
    api_key: SecretStr | None = None


async def _probe_models(base_url: str, api_key: str | None) -> str:
    """Returns <option>s with the models from the endpoint, or an error message."""
    base = base_url.rstrip("/")
    if not base:
        return '<option value="" disabled selected>Enter a base URL</option>'
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    url = f"{base}/models"
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            r = await client.get(url, headers=headers)
            r.raise_for_status()
            data = r.json()
    except httpx.HTTPStatusError as e:
        return f'<option value="" disabled selected>Error {e.response.status_code} — check the key</option>'
    except (httpx.HTTPError, ValueError):
        return '<option value="" disabled selected>Endpoint not accessible</option>'

    ids: list[str] = []
    for m in (data.get("data") if isinstance(data, dict) else None) or []:
        mid = m.get("id") if isinstance(m, dict) else None
        if mid:
            ids.append(mid)
    ids.sort()
    if not ids:
        return '<option value="" disabled selected>(no models returned)</option>'
    return _options_html(ids, ids[0], empty_label="(no models)")


@app.get("/ui/providers/{provider_id}/models", response_class=HTMLResponse)
async def ui_provider_models(provider_id: str) -> str:
    """
    Returns <option>s with the models that the provider's endpoint reports
    on /v1/models. Uses the encrypted key, never exposes it to the client.
    """
    if not crypto_available():
        return '<option value="" disabled selected>Providers not configured</option>'
    try:
        resolved = providers_store.get_decrypted_key(provider_id)
    except CryptoError:
        return '<option value="" disabled selected>Error decrypting the key</option>'
    if resolved is None:
        return '<option value="" disabled selected>Provider not found</option>'
    prov, api_key = resolved
    return await _probe_models(prov.base_url, api_key or None)


@app.post("/ui/models-probe", response_class=HTMLResponse)
async def ui_models_probe(payload: ModelsProbeRequest) -> str:
    """
    Lists models from an OpenAI-compatible endpoint. The front-end calls it
    when creating/editing a provider, after entering base_url + api_key.

    We receive the key by POST (not query) so we do not leave it in access logs.
    """
    key = payload.api_key.get_secret_value() if payload.api_key else None
    return await _probe_models(payload.base_url, key)


# --- provider endpoints (CRUD for saved LLM providers) ---

def _require_crypto() -> None:
    if not crypto_available():
        raise HTTPException(
            503,
            "Provider storage unavailable: MASTER_ENCRYPTION_KEY not configured",
        )


@app.get("/providers")
async def list_providers() -> list[dict]:
    _require_crypto()
    return [p.__dict__ for p in providers_store.list()]


@app.post("/providers", status_code=201)
async def create_provider(payload: ProviderCreate) -> dict:
    _require_crypto()
    try:
        prov = providers_store.create(
            label=payload.label,
            provider_label=payload.provider_label,
            base_url=payload.base_url,
            model=payload.model,
            api_key=payload.api_key.get_secret_value(),
            model_implementer=payload.model_implementer,
            model_reviewer=payload.model_reviewer,
            model_simplifier=payload.model_simplifier,
        )
    except CryptoError as e:
        raise HTTPException(500, f"Cannot seal secret: {e}") from e
    return prov.__dict__


@app.delete("/providers/{provider_id}")
async def delete_provider(provider_id: str) -> dict:
    _require_crypto()
    if not providers_store.delete(provider_id):
        raise HTTPException(404, "Provider not found")
    return {"deleted": True}


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
    """Fires the teardown on the worker (which has docker.sock + worktrees).
    Blocking: waits until it finishes."""
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


# --- HTML dashboard (option A) ---

def _task_row(run_dict: dict) -> dict:
    """Flattens a flow_run dict for the templates."""
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
    """Lists active previews by reading the sidecars. Enriches with flow_run_id
    if it finds the flow run with matching task_id."""
    out = []
    logs_dir = Path(os.environ.get("LOGS_DIR", "/app/logs"))
    for sidecar in sorted(logs_dir.glob("task-*.preview.json")):
        try:
            import json
            data = json.loads(sidecar.read_text(encoding="utf-8"))
            out.append({
                "task_id": data.get("task_id"),
                "url": data.get("url"),
                "port": data.get("port"),
                "flow_run_id": None,  # resolved in dashboard() if possible
            })
        except Exception:
            continue
    return out


@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    raw_tasks = await list_tasks(limit=50)
    tasks = [_task_row(t) for t in raw_tasks]

    # Cross-reference previews with flow runs by task_id so that the teardown
    # button has a valid flow_run_id.
    by_task_id = {t["task_id"]: t["id"] for t in tasks if t.get("task_id") is not None}
    previews = _active_previews()
    for p in previews:
        if p["task_id"] in by_task_id:
            p["flow_run_id"] = by_task_id[p["task_id"]]

    providers = providers_store.list() if crypto_available() else []

    return templates.TemplateResponse("index.html", {
        "request": request,
        "tasks": tasks,
        "previews": previews,
        "default_model": DEFAULT_MODEL,
        "providers": providers,
        "crypto_available": crypto_available(),
    })


@app.get("/ui/tasks", response_class=HTMLResponse)
async def dashboard_tasks_partial(request: Request):
    """Partial for htmx polling."""
    raw_tasks = await list_tasks(limit=50)
    tasks = [_task_row(t) for t in raw_tasks]
    return templates.TemplateResponse("_tasks_list.html", {
        "request": request, "tasks": tasks,
    })


@app.post("/ui/tasks")
async def dashboard_create(
    prompt: str = Form(...),
    repo_path: str = Form(""),
    base_branch: str = Form("main"),
    provider_id: str = Form(""),
    provider_label: str = Form("custom"),
    base_url: str = Form(""),
    model: str = Form(""),
    api_key: str = Form(""),
    model_implementer: str = Form(""),
    model_reviewer: str = Form(""),
    model_simplifier: str = Form(""),
):
    resp = await create_task(TaskCreate(
        prompt=prompt,
        repo_path=repo_path,
        base_branch=base_branch,
        provider_id=provider_id or None,
        provider_label=provider_label,
        base_url=base_url or None,
        model=model or None,
        api_key=SecretStr(api_key) if api_key else None,
        model_implementer=model_implementer or None,
        model_reviewer=model_reviewer or None,
        model_simplifier=model_simplifier or None,
    ))
    return RedirectResponse(url=f"/ui/tasks/{resp.id}", status_code=303)


# --- UI partials for the saved providers panel ---

@app.get("/ui/providers", response_class=HTMLResponse)
async def ui_providers(request: Request):
    providers = providers_store.list() if crypto_available() else []
    return templates.TemplateResponse(
        "_providers_list.html",
        {
            "request": request,
            "providers": providers,
            "crypto_available": crypto_available(),
        },
    )


@app.get("/providers-ui", response_class=HTMLResponse)
async def providers_page(request: Request):
    """Dedicated page for managing LLM providers."""
    providers = providers_store.list() if crypto_available() else []
    return templates.TemplateResponse("providers.html", {
        "request": request,
        "providers": providers,
        "crypto_available": crypto_available(),
        "edit_target": None,
    })


@app.get("/providers-ui/{provider_id}/edit", response_class=HTMLResponse)
async def providers_edit_page(request: Request, provider_id: str):
    """Same template as the creation page but pre-filled."""
    if not crypto_available():
        raise HTTPException(503, "MASTER_ENCRYPTION_KEY not configured")
    target = providers_store.get(provider_id)
    if target is None:
        raise HTTPException(404, "Provider not found")
    return templates.TemplateResponse("providers.html", {
        "request": request,
        "providers": providers_store.list(),
        "crypto_available": True,
        "edit_target": target,
    })


@app.post("/ui/providers")
async def ui_providers_create(
    label: str = Form(...),
    provider_label: str = Form("custom"),
    base_url: str = Form(...),
    model: str = Form(...),
    api_key: str = Form(...),
    model_implementer: str = Form(""),
    model_reviewer: str = Form(""),
    model_simplifier: str = Form(""),
):
    if not crypto_available():
        raise HTTPException(503, "MASTER_ENCRYPTION_KEY not configured")
    providers_store.create(
        label=label,
        provider_label=provider_label,
        base_url=base_url,
        model=model,
        api_key=api_key,
        model_implementer=model_implementer,
        model_reviewer=model_reviewer,
        model_simplifier=model_simplifier,
    )
    return RedirectResponse(url="/providers-ui", status_code=303)


@app.post("/ui/providers/{provider_id}/edit")
async def ui_providers_edit(
    provider_id: str,
    label: str = Form(...),
    provider_label: str = Form("custom"),
    base_url: str = Form(...),
    model: str = Form(...),
    api_key: str = Form(""),
    model_implementer: str = Form(""),
    model_reviewer: str = Form(""),
    model_simplifier: str = Form(""),
):
    if not crypto_available():
        raise HTTPException(503, "MASTER_ENCRYPTION_KEY not configured")
    updated = providers_store.update(
        provider_id=provider_id,
        label=label,
        provider_label=provider_label,
        base_url=base_url,
        model=model,
        api_key=api_key or None,
        model_implementer=model_implementer,
        model_reviewer=model_reviewer,
        model_simplifier=model_simplifier,
    )
    if updated is None:
        raise HTTPException(404, "Provider not found")
    return RedirectResponse(url="/providers-ui", status_code=303)


@app.post("/ui/providers/{provider_id}/delete")
async def ui_providers_delete(provider_id: str):
    if not crypto_available():
        raise HTTPException(503, "MASTER_ENCRYPTION_KEY not configured")
    providers_store.delete(provider_id)
    return RedirectResponse(url="/providers-ui", status_code=303)


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