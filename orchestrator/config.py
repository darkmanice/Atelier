"""Central configuration (host/container duality for bind-mounts)."""
from __future__ import annotations

import os
from pathlib import Path


# --- Paths inside the container ---
PROJECT_ROOT = Path(os.environ.get("APP_ROOT", Path(__file__).parent.parent)).resolve()
WORKTREES_DIR = Path(os.environ.get("WORKTREES_DIR", PROJECT_ROOT / "worktrees")).resolve()
LOGS_DIR = Path(os.environ.get("LOGS_DIR", PROJECT_ROOT / "logs")).resolve()

# --- HOST paths (for bind-mounts in agent containers) ---
WORKTREES_DIR_HOST = Path(
    os.environ.get("WORKTREES_HOST_DIR", str(WORKTREES_DIR))
).resolve()
REPOS_DIR_HOST = Path(os.environ["REPOS_HOST_DIR"]).resolve()

# --- LLM default ---
# Informational only: the real model is chosen by the user per task from
# the front-end. Kept for "default" UI text.
DEFAULT_MODEL = os.environ["DEFAULT_MODEL"]

# --- UID/GID of the agent and runner containers ---
# The worker runs as root (to be able to use the Docker socket), but the
# containers it launches run as 1000. Files that the worker creates in
# bind-mounts inherit UID 0 and must be chown'd again so the agent can
# modify them. See orchestrator/worktree.py::_chown_for_agent.
AGENT_UID = int(os.environ.get("AGENT_UID", "1000"))
AGENT_GID = int(os.environ.get("AGENT_GID", "1000"))

# --- Docker (for launching agents) ---
AGENT_IMAGE = os.environ.get("AGENT_IMAGE", "atelier-agent:latest")
AGENT_NETWORK = os.environ.get("AGENT_NETWORK", "atelier_network")
AGENT_MEM_LIMIT = os.environ.get("AGENT_MEM_LIMIT", "4g")
AGENT_CPU_LIMIT = float(os.environ.get("AGENT_CPU_LIMIT", "2.0"))
AGENT_TIMEOUT_SEC = int(os.environ.get("AGENT_TIMEOUT_SEC", "2400"))

# --- FSM ---
MAX_REVIEW_ITERATIONS = int(os.environ.get("MAX_REVIEW_ITERATIONS", "3"))


def container_path_for_worktree(task_id: int) -> Path:
    return WORKTREES_DIR / f"task-{task_id}"


def host_path_for_worktree(task_id: int) -> Path:
    return WORKTREES_DIR_HOST / f"task-{task_id}"
