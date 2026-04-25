"""
Long-lived agent containers: one per (task_id, role) for the whole flow run.

First invocation creates an idle container (`sleep infinity`) labeled with
`atelier.task=<task_id>` and `atelier.role=<role>`. Subsequent invocations
(retries) reuse it via `docker exec`. The flow's `cleanup-containers` task
removes everything matching the task label at the end of the run.
"""
from __future__ import annotations

import logging
import os
import threading
from pathlib import Path

import docker
from docker.errors import ImageNotFound, NotFound

from orchestrator.branch_guard import diff_refs, snapshot_refs
from orchestrator.config import (
    AGENT_CPU_LIMIT,
    AGENT_GID,
    AGENT_IMAGE,
    AGENT_MEM_LIMIT,
    AGENT_NETWORK,
    AGENT_TIMEOUT_SEC,
    AGENT_UID,
    host_path_for_project,
)

from agents.models import AgentResult, TaskInput

log = logging.getLogger(__name__)

_client = docker.from_env()


class ContainerRunError(RuntimeError):
    pass


def _container_name(task_id: int, role_value: str) -> str:
    return f"atelier-agent-task-{task_id}-{role_value}"


def _ensure_agent_container(
    name: str,
    task_id: int,
    role_value: str,
    host_worktree_path: Path,
    host_project_path: Path,
    container_repo_path: Path,
):
    """Return a running container by name, creating one on first call.

    A container in any state other than `running` is removed and recreated:
    if it died mid-task its in-memory state is suspect anyway, and the next
    `exec_run` would fail outright.
    """
    try:
        existing = _client.containers.get(name)
        existing.reload()
        if existing.status == "running":
            return existing
        log.info("Existing agent container %s is %s; recreating", name, existing.status)
        try:
            existing.remove(force=True)
        except Exception:
            pass
    except NotFound:
        pass

    log.info("Creating long-lived agent container %s", name)
    return _client.containers.run(
        image=AGENT_IMAGE,
        name=name,
        command=["sleep", "infinity"],
        detach=True,
        user=f"{AGENT_UID}:{AGENT_GID}",
        labels={
            "atelier.task": str(task_id),
            "atelier.role": role_value,
        },
        volumes={
            str(host_worktree_path): {"bind": "/workspace", "mode": "rw"},
            str(host_project_path): {"bind": str(container_repo_path), "mode": "rw"},
        },
        working_dir="/workspace",
        mem_limit=AGENT_MEM_LIMIT,
        nano_cpus=int(AGENT_CPU_LIMIT * 1_000_000_000),
        network=AGENT_NETWORK,
        auto_remove=False,
    )


def _exec_with_timeout(
    container,
    cmd: list[str],
    environment: dict,
    timeout_sec: int,
) -> tuple[int, str, str, bool]:
    """Run `cmd` in `container` via docker exec with a hard wall-clock timeout.

    Returns (exit_code, stdout, stderr, timed_out). On timeout the container
    is killed (the next call will recreate it), since docker-py exposes no
    way to cancel a single in-flight exec.
    """
    api = _client.api
    exec_id = api.exec_create(
        container.id,
        cmd=cmd,
        environment=environment,
        workdir="/workspace",
        user=f"{AGENT_UID}:{AGENT_GID}",
    )["Id"]

    box: dict = {}

    def _runner() -> None:
        try:
            box["out"] = api.exec_start(exec_id, stream=False, demux=True)
        except Exception as e:
            box["err"] = e

    t = threading.Thread(target=_runner, daemon=True)
    t.start()
    t.join(timeout=timeout_sec)

    if t.is_alive():
        log.warning("exec %s on %s timed out after %ss; killing container",
                    exec_id[:12], container.name, timeout_sec)
        try:
            container.kill()
        except Exception:
            pass
        return -1, "", f"Agent exec timed out after {timeout_sec}s", True

    if "err" in box:
        raise box["err"]

    stdout_bytes, stderr_bytes = box.get("out") or (b"", b"")
    stdout = (stdout_bytes or b"").decode("utf-8", errors="replace")
    stderr = (stderr_bytes or b"").decode("utf-8", errors="replace")

    inspect = api.exec_inspect(exec_id)
    exit_code = inspect.get("ExitCode")
    if exit_code is None:
        exit_code = -1
    return exit_code, stdout, stderr, False


def run_agent(
    task_input: TaskInput,
    host_worktree_path: Path,
    container_worktree_path: Path,
    container_repo_path: Path,
    api_key: str = "",
) -> AgentResult:
    try:
        _client.images.get(AGENT_IMAGE)
    except ImageNotFound as e:
        raise ContainerRunError(
            f"Image {AGENT_IMAGE} not found. Rebuild with: docker compose build agent-builder"
        ) from e

    host_project_path = host_path_for_project(container_repo_path)
    name = _container_name(task_input.task_id, task_input.role.value)

    container = _ensure_agent_container(
        name=name,
        task_id=task_input.task_id,
        role_value=task_input.role.value,
        host_worktree_path=host_worktree_path,
        host_project_path=host_project_path,
        container_repo_path=container_repo_path,
    )

    # Write input to a file inside the worktree (visible from the agent at /workspace)
    # NOTE: `api_key` NEVER goes into task_input; it is injected separately as an env var.
    input_file = container_worktree_path / ".task-input.json"
    input_file.write_text(task_input.model_dump_json(), encoding="utf-8")
    os.chmod(input_file, 0o644)
    # The worker writes it as root; the agent runs as UID 1000 and must
    # be able to unlink() it (entrypoint.py deletes it before starting).
    try:
        os.chown(input_file, AGENT_UID, AGENT_GID)
    except PermissionError:
        pass

    log.info(
        "Invoking agent role=%s task=%s in container %s",
        task_input.role, task_input.task_id, name,
    )

    # Branch sandbox: capture every ref of the project. Anything outside
    # refs/heads/<feature_branch> that changes during the run is a violation.
    pre_refs = snapshot_refs(container_repo_path)

    # Per-invocation env. Critical: the user can change models / providers
    # between attempts, so we MUST set these on every exec, not just at
    # container creation.
    env = {
        "AGENT_ROLE": task_input.role.value,
        "TASK_ID": str(task_input.task_id),
        "TASK_INPUT_FILE": "/workspace/.task-input.json",
        "OPENAI_API_BASE": task_input.base_url,
        "OPENAI_API_KEY": api_key or "sk-no-auth",
    }
    # Forward optional aider tuning knobs from the worker env. None of these
    # are required; if unset, aider uses its defaults (or the implementer's).
    for key in ("AIDER_EDIT_FORMAT",):
        if value := os.environ.get(key):
            env[key] = value

    exit_code, stdout, stderr, timed_out = _exec_with_timeout(
        container,
        cmd=["python", "-m", "agents.entrypoint"],
        environment=env,
        timeout_sec=AGENT_TIMEOUT_SEC,
    )

    if timed_out:
        return AgentResult(
            success=False,
            verdict="failed",
            summary=f"Agent container timed out after {AGENT_TIMEOUT_SEC}s",
            log=[],
        )

    if stderr:
        log.debug("Agent stderr:\n%s", stderr[-4000:])

    result = _parse_agent_output(stdout)
    if result is None:
        log.error("Could not parse agent output. stdout was:\n%s", stdout[-2000:])
        return AgentResult(
            success=False,
            verdict="failed",
            summary=f"Could not parse agent output (exit code {exit_code})",
            log=[],
        )

    # Branch sandbox verification. Any ref change outside the feature
    # branch (delete, move, or create) aborts the task regardless of
    # whatever the agent "said" it did.
    violations = diff_refs(
        pre_refs,
        snapshot_refs(container_repo_path),
        task_input.feature_branch,
    )
    if violations:
        log.error(
            "Ref sandbox violation (role=%s task=%s): %s",
            task_input.role, task_input.task_id, violations,
        )
        return AgentResult(
            success=False,
            verdict="failed",
            summary="Branch sandbox violation: " + "; ".join(violations),
            log=[],
        )
    return result


def cleanup_task_containers(task_id: int) -> int:
    """Remove every container labeled atelier.task=<task_id>. Idempotent.

    Returns the count of containers removed. Errors per container are
    swallowed (best-effort cleanup).
    """
    removed = 0
    try:
        containers = _client.containers.list(
            all=True, filters={"label": f"atelier.task={task_id}"}
        )
    except Exception as e:
        log.warning("Failed to list containers for cleanup of task %s: %s", task_id, e)
        return 0
    for c in containers:
        try:
            c.remove(force=True)
            removed += 1
        except Exception as e:
            log.warning("Failed to remove %s: %s", c.name, e)
    if removed:
        log.info("Cleaned up %d container(s) for task %s", removed, task_id)
    return removed


def _parse_agent_output(stdout: str) -> AgentResult | None:
    for line in reversed(stdout.strip().splitlines()):
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            return AgentResult.model_validate_json(line)
        except Exception:
            continue
    return None
