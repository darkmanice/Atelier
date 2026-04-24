"""
Service setup/teardown for E2E tests. Run FROM the Prefect worker (which has
access to the Docker socket), not from inside the runner.

Why: setup/teardown commands are typically `docker compose up/down` that
need the host's Docker. The worker has it via /var/run/docker.sock.

Commands are run with `cwd` equal to the host worktree, so that
`docker compose up` finds its docker-compose.yml.
"""
from __future__ import annotations

import logging
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)


@dataclass
class ServiceResult:
    success: bool
    stdout: str
    stderr: str


def run_service_command(
    command: str,
    host_worktree_path: Path,
    timeout_sec: int = 300,
    env: dict[str, str] | None = None,
) -> ServiceResult:
    """
    Runs a shell command with cwd in the host worktree (from the worker).
    If `env` is passed, it is merged over os.environ (the passed values win).
    """
    log.info("Running service command: %s (cwd=%s)", command, host_worktree_path)
    full_env = os.environ.copy()
    if env:
        full_env.update(env)
    try:
        result = subprocess.run(
            command,
            shell=True,
            cwd=str(host_worktree_path),
            capture_output=True,
            text=True,
            timeout=timeout_sec,
            env=full_env,
        )
        return ServiceResult(
            success=(result.returncode == 0),
            stdout=result.stdout,
            stderr=result.stderr,
        )
    except subprocess.TimeoutExpired as e:
        return ServiceResult(
            success=False,
            stdout="",
            stderr=f"Service command timed out after {timeout_sec}s: {e}",
        )
    except Exception as e:
        return ServiceResult(
            success=False,
            stdout="",
            stderr=f"Service command raised: {type(e).__name__}: {e}",
        )
