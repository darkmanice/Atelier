"""Wrapper over git worktree with container/host duality."""
from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

from orchestrator.config import (
    AGENT_GID,
    AGENT_UID,
    WORKTREES_DIR,
    container_path_for_worktree,
    host_path_for_worktree,
)


class WorktreeError(RuntimeError):
    pass


@dataclass
class WorktreeHandle:
    container_path: Path
    host_path: Path


def _run(cmd: list[str], cwd: Path | str | None = None) -> str:
    try:
        result = subprocess.run(
            cmd, cwd=cwd, capture_output=True, text=True, check=True, timeout=60
        )
        return result.stdout
    except subprocess.CalledProcessError as e:
        raise WorktreeError(f"{' '.join(cmd)}: {e.stderr.strip()}") from e


def create_worktree(
    repo_path: Path, task_id: int, base_branch: str, feature_branch: str
) -> WorktreeHandle:
    container_path = container_path_for_worktree(task_id)
    host_path = host_path_for_worktree(task_id)

    WORKTREES_DIR.mkdir(parents=True, exist_ok=True)

    if container_path.exists():
        remove_worktree(repo_path, container_path)

    _run(["git", "fetch", "--all", "--prune"], cwd=repo_path)

    # If the branch already exists (previous task), reuse. Otherwise, create.
    branches = _run(["git", "branch", "--all"], cwd=repo_path)
    branch_exists = any(
        feature_branch == b.strip().lstrip("* ").replace("remotes/origin/", "")
        for b in branches.splitlines()
    )

    if branch_exists:
        # We delete the branch to avoid conflicts (tasks are ephemeral for now)
        _run(["git", "branch", "-D", feature_branch], cwd=repo_path)

    _run(
        ["git", "worktree", "add", "-b", feature_branch, str(container_path), base_branch],
        cwd=repo_path,
    )

    # The worker runs as root, but agent and runner containers run as UID
    # 1000. Without this chown, the agent cannot write `app.py`, nor can
    # `git add -A` write in `<repo>/.git/worktrees/<name>/index`, and the
    # entrypoint cannot delete `.task-input.json`.
    admin_dir = repo_path / ".git" / "worktrees" / container_path.name
    _chown_for_agent(container_path)
    if admin_dir.exists():
        _chown_for_agent(admin_dir)

    return WorktreeHandle(container_path=container_path, host_path=host_path)


def _chown_for_agent(path: Path) -> None:
    """Recursive chown to the agent's UID/GID. No-op if we are not root."""
    try:
        os.chown(path, AGENT_UID, AGENT_GID)
    except PermissionError:
        return
    except FileNotFoundError:
        return
    for root, dirs, files in os.walk(path):
        for name in dirs + files:
            child = os.path.join(root, name)
            try:
                os.chown(child, AGENT_UID, AGENT_GID, follow_symlinks=False)
            except (PermissionError, FileNotFoundError, OSError):
                pass


def remove_worktree(repo_path: Path, container_path: Path) -> None:
    try:
        _run(["git", "worktree", "remove", "--force", str(container_path)], cwd=repo_path)
    except WorktreeError:
        if container_path.exists():
            subprocess.run(["rm", "-rf", str(container_path)], check=False)


def get_diff_summary(container_path: Path, base_branch: str) -> str:
    try:
        return _run(["git", "diff", "--stat", f"{base_branch}...HEAD"], cwd=container_path)
    except WorktreeError:
        return "(could not compute diff)"
