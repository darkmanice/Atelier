"""Central configuration.

Path-aliased bind mounts: every container mounts `projects/`, `worktrees/`,
`logs/` and `data/` at the host's absolute path (e.g.
`/home/me/atelier/worktrees/task-X`), not at a container-relative alias
like `/workspace` or `/projects`. As a result, paths are identical on
the host and inside containers, and there is no host/container duality
to translate.

The translator helpers below (`host_path_for_project`,
`host_path_for_worktree`, `container_path_for_worktree`) are kept as
identity functions so existing call sites don't have to change. They
still serve as documentation of "what a path semantically is".
"""
from __future__ import annotations

import os
from pathlib import Path


# --- Paths (identical on host and inside containers) ---
PROJECT_ROOT = Path(os.environ.get("APP_ROOT", Path(__file__).parent.parent)).resolve()
WORKTREES_DIR = Path(os.environ.get("WORKTREES_DIR", PROJECT_ROOT / "worktrees")).resolve()
LOGS_DIR = Path(os.environ.get("LOGS_DIR", PROJECT_ROOT / "logs")).resolve()
PROJECTS_ROOT = Path(os.environ.get("PROJECTS_ROOT", PROJECT_ROOT / "projects")).resolve()


def worktree_path(task_id: int) -> Path:
    """Absolute path of the worktree for `task_id`. Identical on host
    and inside containers thanks to path-aliased bind mounts."""
    return WORKTREES_DIR / f"task-{task_id}"


# Identity aliases kept for clarity at call sites that historically
# distinguished host vs container paths. They all resolve to the same
# absolute path on disk.
container_path_for_worktree = worktree_path
host_path_for_worktree = worktree_path


def host_path_for_project(repo_path: Path) -> Path:
    """Identity: a project path is the same on host and in containers."""
    return Path(repo_path).resolve()


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
