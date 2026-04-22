"""
Setup/teardown de servicios para tests E2E. Ejecutado DESDE el worker de Prefect
(que tiene acceso al socket Docker), no desde dentro del runner.

Por qué: los comandos setup/teardown típicamente son `docker compose up/down`
que necesitan Docker del host. El worker lo tiene via /var/run/docker.sock.

Los comandos se ejecutan con `cwd` igual al worktree del host, para que
`docker compose up` encuentre su docker-compose.yml.
"""
from __future__ import annotations

import logging
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
    command: str, host_worktree_path: Path, timeout_sec: int = 300
) -> ServiceResult:
    """Ejecuta un comando shell con cwd en el worktree del host (desde el worker)."""
    log.info("Running service command: %s (cwd=%s)", command, host_worktree_path)
    try:
        result = subprocess.run(
            command,
            shell=True,
            cwd=str(host_worktree_path),
            capture_output=True,
            text=True,
            timeout=timeout_sec,
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
