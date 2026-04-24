"""
Runs test commands inside ephemeral containers over the worktree.

Two runner images:
  - atelier-runner-quick:   Python + pytest + linters. For quick/full tests.
  - atelier-runner-e2e:     based on mcr.microsoft.com/playwright. For E2E.

The runner is deterministic: does not use an LLM. It just runs a command and
captures its exit code + output. If exit==0 -> success. Otherwise -> failure
with the output.
"""
from __future__ import annotations

import logging
import os
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

import docker
from docker.errors import ImageNotFound

from orchestrator.config import (
    AGENT_CPU_LIMIT,
    AGENT_MEM_LIMIT,
    AGENT_NETWORK,
    REPOS_DIR_HOST,
)

log = logging.getLogger(__name__)

_client = docker.from_env()

# Runner images (built by compose at startup)
RUNNER_QUICK_IMAGE = os.environ.get(
    "RUNNER_QUICK_IMAGE", "atelier-runner-quick:latest"
)
RUNNER_E2E_IMAGE = os.environ.get(
    "RUNNER_E2E_IMAGE", "atelier-runner-e2e:latest"
)


@dataclass
class RunnerResult:
    """Result of running a command in a runner."""

    success: bool
    exit_code: int
    stdout: str
    stderr: str
    duration_seconds: float
    command: str
    skipped: bool = False
    skip_reason: str | None = None

    def summary_for_feedback(self) -> str:
        """Concise format to pass to the implementer as feedback."""
        if self.skipped:
            return f"(skipped: {self.skip_reason})"
        head = f"Command: {self.command}\nExit code: {self.exit_code}\n"
        # We prioritize stderr over stdout for feedback (failures usually go there)
        body = (self.stderr or self.stdout)[-4000:]
        return head + "\nOutput:\n" + body


def _ensure_image(image: str) -> None:
    try:
        _client.images.get(image)
    except ImageNotFound as e:
        raise RuntimeError(
            f"Runner image {image} not found. Rebuild with: docker compose build"
        ) from e


def run_command_in_runner(
    image: str,
    command: str,
    host_worktree_path: Path,
    timeout_sec: int,
    extra_env: dict | None = None,
    mount_docker_socket: bool = False,
) -> RunnerResult:
    """
    Runs a shell command inside an ephemeral container of the given runner.

    Args:
        image: Runner image.
        command: Full shell command (passed to `sh -c`).
        host_worktree_path: Path of the worktree ON THE HOST (for bind mount).
        timeout_sec: Hard timeout.
        extra_env: Additional environment variables.
        mount_docker_socket: If True, mounts /var/run/docker.sock
                             (DO NOT use for normal tests; use for setup/teardown
                             of E2E services if it were ever needed — currently
                             services are brought up by Prefect from outside).
    """
    _ensure_image(image)

    env = {
        "CI": "true",
        "PYTHONUNBUFFERED": "1",
    }
    if extra_env:
        env.update(extra_env)

    volumes: dict = {
        str(host_worktree_path): {"bind": "/workspace", "mode": "rw"},
        str(REPOS_DIR_HOST): {"bind": "/repos", "mode": "rw"},
    }
    if mount_docker_socket:
        volumes["/var/run/docker.sock"] = {
            "bind": "/var/run/docker.sock", "mode": "rw"
        }

    # Unique name (there can be several concurrent runners per task)
    unique = uuid.uuid4().hex[:8]
    container = _client.containers.create(
        image=image,
        command=["sh", "-c", command],
        detach=True,
        environment=env,
        volumes=volumes,
        working_dir="/workspace",
        mem_limit=AGENT_MEM_LIMIT,
        nano_cpus=int(AGENT_CPU_LIMIT * 1_000_000_000),
        network=AGENT_NETWORK,
        auto_remove=False,
        name=f"atelier-runner-{unique}",
    )

    start_time = time.time()

    try:
        container.start()

        try:
            exit_info = container.wait(timeout=timeout_sec)
            exit_code = exit_info.get("StatusCode", -1)
        except Exception as e:
            log.warning("Runner timed out, killing: %s", e)
            try:
                container.kill()
            except Exception:
                pass
            duration = time.time() - start_time
            return RunnerResult(
                success=False,
                exit_code=-1,
                stdout="",
                stderr=f"Runner timed out after {timeout_sec}s",
                duration_seconds=duration,
                command=command,
            )

        duration = time.time() - start_time
        stdout = container.logs(stdout=True, stderr=False).decode("utf-8", errors="replace")
        stderr = container.logs(stdout=False, stderr=True).decode("utf-8", errors="replace")

        return RunnerResult(
            success=(exit_code == 0),
            exit_code=exit_code,
            stdout=stdout,
            stderr=stderr,
            duration_seconds=duration,
            command=command,
        )

    finally:
        try:
            container.remove(force=True)
        except Exception:
            pass


def skipped(reason: str, command: str = "") -> RunnerResult:
    """Helper to create a RunnerResult that represents a skip."""
    return RunnerResult(
        success=True,
        exit_code=0,
        stdout="",
        stderr="",
        duration_seconds=0,
        command=command,
        skipped=True,
        skip_reason=reason,
    )
