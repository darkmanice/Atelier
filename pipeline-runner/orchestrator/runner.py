"""
Ejecuta comandos de test dentro de contenedores efímeros sobre el worktree.

Dos imágenes de runner:
  - pipeline-runner-quick:   Python + pytest + linters. Para quick/full tests.
  - pipeline-runner-e2e:     basado en mcr.microsoft.com/playwright. Para E2E.

El runner es determinista: no usa LLM. Solo lanza un comando y captura su
exit code + output. Si exit==0 → success. Si no → failure con el output.
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

# Imágenes de runners (construidas por el compose al arrancar)
RUNNER_QUICK_IMAGE = os.environ.get(
    "PIPELINE_RUNNER_QUICK_IMAGE", "pipeline-runner-quick:latest"
)
RUNNER_E2E_IMAGE = os.environ.get(
    "PIPELINE_RUNNER_E2E_IMAGE", "pipeline-runner-e2e:latest"
)


@dataclass
class RunnerResult:
    """Resultado de ejecutar un comando en un runner."""

    success: bool
    exit_code: int
    stdout: str
    stderr: str
    duration_seconds: float
    command: str
    skipped: bool = False
    skip_reason: str | None = None

    def summary_for_feedback(self) -> str:
        """Formato conciso para pasar al implementer como feedback."""
        if self.skipped:
            return f"(skipped: {self.skip_reason})"
        head = f"Command: {self.command}\nExit code: {self.exit_code}\n"
        # Priorizamos stderr sobre stdout para feedback (los fallos suelen ir ahí)
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
    Ejecuta un comando shell dentro de un contenedor efímero del runner dado.

    Args:
        image: Imagen del runner.
        command: Comando shell completo (se pasa a `sh -c`).
        host_worktree_path: Ruta del worktree EN EL HOST (para bind mount).
        timeout_sec: Timeout duro.
        extra_env: Variables de entorno adicionales.
        mount_docker_socket: Si True, monta /var/run/docker.sock
                             (NO usar para tests normales; sí para setup/teardown
                             de servicios E2E si llegara a hacer falta — actualmente
                             los services los levanta Prefect desde fuera).
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

    # Nombre único (puede haber varios runners concurrentes por tarea)
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
        name=f"pipeline-runner-{unique}",
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
    """Helper para crear un RunnerResult que representa un skip."""
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
