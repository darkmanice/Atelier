"""
Launches ephemeral agent containers. Final working version:
input via file (not stdin), dual paths, compose network.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path

import docker
from docker.errors import ImageNotFound

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
        "Launching agent container role=%s task=%s",
        task_input.role, task_input.task_id,
    )

    # Sandbox: mount ONLY the specific project at the same path the worker sees
    # it (needed because the worktree's .git gitdir pointer references that path
    # absolutely). The agent has no visibility of sibling projects.
    host_project_path = host_path_for_project(container_repo_path)

    # Branch sandbox: capture every ref of the project. Anything outside
    # refs/heads/<feature_branch> that changes during the run is a violation.
    pre_refs = snapshot_refs(container_repo_path)

    # LLM config to the agent via standard OpenAI SDK + LiteLLM env vars.
    # The key only lives in the container process during its execution
    # (auto_remove=False here, but we remove it in the finally below).
    container = _client.containers.create(
        image=AGENT_IMAGE,
        detach=True,
        user=f"{AGENT_UID}:{AGENT_GID}",
        environment={
            "AGENT_ROLE": task_input.role.value,
            "TASK_ID": str(task_input.task_id),
            "TASK_INPUT_FILE": "/workspace/.task-input.json",
            "OPENAI_API_BASE": task_input.base_url,
            "OPENAI_API_KEY": api_key or "sk-no-auth",
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
        name=f"atelier-agent-task-{task_input.task_id}-{task_input.role.value}",
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
