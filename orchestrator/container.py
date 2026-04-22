"""
Lanza contenedores efímeros de agente. Versión final que ya funciona:
input por fichero (no stdin), paths duales, red del compose.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path

import docker
from docker.errors import ImageNotFound

from orchestrator.config import (
    AGENT_CPU_LIMIT,
    AGENT_GID,
    AGENT_IMAGE,
    AGENT_MEM_LIMIT,
    AGENT_NETWORK,
    AGENT_TIMEOUT_SEC,
    AGENT_UID,
    REPOS_DIR_HOST,
)

from agents.models import AgentResult, TaskInput

log = logging.getLogger(__name__)

_client = docker.from_env()


class ContainerRunError(RuntimeError):
    pass


def run_agent(
    task_input: TaskInput, host_worktree_path: Path, container_worktree_path: Path
) -> AgentResult:
    try:
        _client.images.get(AGENT_IMAGE)
    except ImageNotFound as e:
        raise ContainerRunError(
            f"Image {AGENT_IMAGE} not found. Rebuild with: docker compose build agent-builder"
        ) from e

    # Escribir input en fichero dentro del worktree (visible desde el agente en /workspace)
    input_file = container_worktree_path / ".task-input.json"
    input_file.write_text(task_input.model_dump_json(), encoding="utf-8")
    os.chmod(input_file, 0o644)
    # El worker lo escribe como root; el agente corre como UID 1000 y debe
    # poder unlink()earlo (entrypoint.py lo borra antes de arrancar).
    try:
        os.chown(input_file, AGENT_UID, AGENT_GID)
    except PermissionError:
        pass

    log.info(
        "Launching agent container role=%s task=%s",
        task_input.role, task_input.task_id,
    )

    container = _client.containers.create(
        image=AGENT_IMAGE,
        detach=True,
        environment={
            "AGENT_ROLE": task_input.role.value,
            "TASK_ID": str(task_input.task_id),
            "TASK_INPUT_FILE": "/workspace/.task-input.json",
        },
        volumes={
            str(host_worktree_path): {"bind": "/workspace", "mode": "rw"},
            str(REPOS_DIR_HOST): {"bind": "/repos", "mode": "rw"},
        },
        working_dir="/workspace",
        mem_limit=AGENT_MEM_LIMIT,
        nano_cpus=int(AGENT_CPU_LIMIT * 1_000_000_000),
        network=AGENT_NETWORK,
        auto_remove=False,
        name=f"pipeline-agent-task-{task_input.task_id}-{task_input.role.value}",
    )

    try:
        container.start()

        try:
            exit_info = container.wait(timeout=AGENT_TIMEOUT_SEC)
            exit_code = exit_info.get("StatusCode", -1)
        except Exception as e:
            log.warning("Container timed out, killing: %s", e)
            try:
                container.kill()
            except Exception:
                pass
            return AgentResult(
                success=False,
                verdict="failed",
                summary=f"Agent container timed out after {AGENT_TIMEOUT_SEC}s",
                log=[],
            )

        stdout = container.logs(stdout=True, stderr=False).decode("utf-8", errors="replace")
        stderr = container.logs(stdout=False, stderr=True).decode("utf-8", errors="replace")

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
        return result

    finally:
        try:
            container.remove(force=True)
        except Exception:
            pass


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
