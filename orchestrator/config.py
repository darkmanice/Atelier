"""
Configuración central. Igual que en el proyecto anterior (dualidad host/container).
"""
from __future__ import annotations

import os
from pathlib import Path


# --- Paths dentro del contenedor ---
PROJECT_ROOT = Path(os.environ.get("PIPELINE_ROOT", Path(__file__).parent.parent)).resolve()
WORKTREES_DIR = Path(os.environ.get("PIPELINE_WORKTREES", PROJECT_ROOT / "worktrees")).resolve()
LOGS_DIR = Path(os.environ.get("PIPELINE_LOGS", PROJECT_ROOT / "logs")).resolve()

# --- Paths del HOST (para bind-mounts en contenedores-agente) ---
WORKTREES_DIR_HOST = Path(
    os.environ.get("PIPELINE_WORKTREES_HOST", str(WORKTREES_DIR))
).resolve()
REPOS_DIR_HOST = Path(os.environ.get("PIPELINE_REPOS_HOST", str(PROJECT_ROOT / "repos"))).resolve()

# --- Ollama (externo, Windows host) ---
OLLAMA_HOST_FROM_CONTAINER = os.environ.get(
    "OLLAMA_HOST_FROM_CONTAINER", "http://172.21.192.1:11434"
)
DEFAULT_MODEL = os.environ.get("PIPELINE_MODEL", "gemma4:26b")

# --- UID/GID de los contenedores de agente y runner ---
# El worker corre como root (para poder usar el socket Docker), pero los
# contenedores que lanza corren como 1000. Los ficheros que el worker crea en
# bind-mounts heredan UID 0 y hay que re-hacer chown para que el agente pueda
# modificarlos. Ver orchestrator/worktree.py::_chown_for_agent.
AGENT_UID = int(os.environ.get("PIPELINE_AGENT_UID", "1000"))
AGENT_GID = int(os.environ.get("PIPELINE_AGENT_GID", "1000"))

# --- Docker (para lanzar agentes) ---
AGENT_IMAGE = os.environ.get("PIPELINE_AGENT_IMAGE", "pipeline-agent:latest")
AGENT_NETWORK = os.environ.get("PIPELINE_AGENT_NETWORK", "pipeline-ia-prefect_pipeline")
AGENT_MEM_LIMIT = os.environ.get("PIPELINE_AGENT_MEM", "4g")
AGENT_CPU_LIMIT = float(os.environ.get("PIPELINE_AGENT_CPUS", "2.0"))
AGENT_TIMEOUT_SEC = int(os.environ.get("PIPELINE_AGENT_TIMEOUT", "2400"))

# --- FSM ---
MAX_REVIEW_ITERATIONS = int(os.environ.get("PIPELINE_MAX_REVIEWS", "3"))


def container_path_for_worktree(task_id: int) -> Path:
    return WORKTREES_DIR / f"task-{task_id}"


def host_path_for_worktree(task_id: int) -> Path:
    return WORKTREES_DIR_HOST / f"task-{task_id}"
