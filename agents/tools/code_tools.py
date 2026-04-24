"""
Tools that the reviewer and simplifier can invoke.

The implementer does NOT use this module because it delegates to Aider, which
already brings its own more sophisticated toolset (with RAG over the repo,
patch application, etc.)

The definitions follow the Ollama tool-calling format (OpenAI-compatible).
Each function returns a string (what the agent will see on the next turn).
"""
from __future__ import annotations

import subprocess
from pathlib import Path

MAX_FILE_CHARS = 20_000          # truncate huge files when reading
MAX_DIFF_CHARS = 60_000          # truncate huge diffs
MAX_LIST_FILES = 200             # truncate listings


# -----------------------
# Definitions (schema)
# -----------------------

TOOL_DEFINITIONS = [
    {
        "type": "function",
        "function": {
            "name": "list_files",
            "description": "Lists the files in the worktree. Useful for orienting yourself at the start.",
            "parameters": {
                "type": "object",
                "properties": {
                    "subpath": {
                        "type": "string",
                        "description": "Subpath relative to the worktree. Empty = root.",
                    }
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Reads the content of a file in the worktree.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Path relative to the worktree.",
                    }
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_diff",
            "description": "Returns the diff between the current branch and the base branch. Useful for review.",
            "parameters": {
                "type": "object",
                "properties": {
                    "base_branch": {"type": "string", "description": "Base branch (e.g. 'main')."},
                },
                "required": ["base_branch"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "Overwrites a file. Only use in simplifier role. Creates directories if needed.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "content": {"type": "string"},
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "git_commit",
            "description": "Commits the current changes with the given message.",
            "parameters": {
                "type": "object",
                "properties": {
                    "message": {"type": "string"},
                },
                "required": ["message"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "finish",
            "description": (
                "End the task. "
                "For reviewer: verdict='approved' or 'changes_requested' + comments. "
                "For simplifier: verdict='done' + summary."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "verdict": {
                        "type": "string",
                        "enum": ["approved", "changes_requested", "done"],
                    },
                    "summary": {"type": "string"},
                    "comments": {"type": "string", "description": "Detailed feedback."},
                },
                "required": ["verdict", "summary"],
            },
        },
    },
]


# -----------------------
# Implementations
# -----------------------


def _safe_path(worktree: Path, relative: str) -> Path:
    """Prevents escaping the worktree via malicious relative paths."""
    target = (worktree / relative).resolve()
    worktree_resolved = worktree.resolve()
    if not str(target).startswith(str(worktree_resolved)):
        raise ValueError(f"Path {relative!r} escapes worktree")
    return target


def list_files(worktree: Path, subpath: str = "") -> str:
    base = _safe_path(worktree, subpath) if subpath else worktree
    if not base.exists():
        return f"ERROR: {subpath!r} does not exist"
    out: list[str] = []
    for i, p in enumerate(sorted(base.rglob("*"))):
        if ".git" in p.parts or "node_modules" in p.parts or ".venv" in p.parts:
            continue
        if p.is_file():
            out.append(str(p.relative_to(worktree)))
        if i >= MAX_LIST_FILES:
            out.append(f"... (truncated at {MAX_LIST_FILES})")
            break
    return "\n".join(out) if out else "(empty)"


def read_file(worktree: Path, path: str) -> str:
    target = _safe_path(worktree, path)
    if not target.is_file():
        return f"ERROR: {path!r} is not a file"
    try:
        content = target.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return f"ERROR: {path!r} is not text (binary file)"
    if len(content) > MAX_FILE_CHARS:
        return content[:MAX_FILE_CHARS] + f"\n\n... (truncated, {len(content) - MAX_FILE_CHARS} chars omitted)"
    return content


def get_diff(worktree: Path, base_branch: str) -> str:
    try:
        result = subprocess.run(
            ["git", "diff", f"{base_branch}...HEAD"],
            cwd=worktree,
            capture_output=True,
            text=True,
            timeout=30,
            check=True,
        )
    except subprocess.CalledProcessError as e:
        return f"ERROR running git diff: {e.stderr}"
    diff = result.stdout
    if len(diff) > MAX_DIFF_CHARS:
        return diff[:MAX_DIFF_CHARS] + f"\n\n... (truncated, {len(diff) - MAX_DIFF_CHARS} chars omitted)"
    return diff or "(no changes)"


def write_file(worktree: Path, path: str, content: str) -> str:
    target = _safe_path(worktree, path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    return f"wrote {len(content)} chars to {path}"


def _current_branch(worktree: Path) -> str | None:
    """Return the branch name of HEAD, or None if detached/error."""
    try:
        return subprocess.run(
            ["git", "symbolic-ref", "--short", "HEAD"],
            cwd=worktree, capture_output=True, text=True, check=True, timeout=10,
        ).stdout.strip()
    except subprocess.CalledProcessError:
        return None


def git_commit(worktree: Path, message: str, feature_branch: str) -> str:
    # Branch sandbox: the agent is only allowed to commit on its task branch.
    # If HEAD is elsewhere (detached, or on master/main via checkout), refuse.
    current = _current_branch(worktree)
    if current != feature_branch:
        return (
            f"ERROR: commits are restricted to branch '{feature_branch}'; "
            f"HEAD is on '{current or '(detached)'}'."
        )
    try:
        subprocess.run(["git", "add", "-A"], cwd=worktree, check=True, timeout=30)
        subprocess.run(
            ["git", "commit", "-m", message, "--allow-empty"],
            cwd=worktree,
            capture_output=True,
            text=True,
            timeout=30,
            check=True,
        )
        sha = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=worktree,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        return f"committed {sha}: {message}"
    except subprocess.CalledProcessError as e:
        return f"ERROR: {e.stderr or e.stdout}"


# Dispatcher invoked by the ReAct loop
def dispatch(name: str, arguments: dict, worktree: Path, feature_branch: str) -> str:
    handlers = {
        "list_files": lambda: list_files(worktree, arguments.get("subpath", "")),
        "read_file": lambda: read_file(worktree, arguments["path"]),
        "get_diff": lambda: get_diff(worktree, arguments["base_branch"]),
        "write_file": lambda: write_file(worktree, arguments["path"], arguments["content"]),
        "git_commit": lambda: git_commit(worktree, arguments["message"], feature_branch),
    }
    handler = handlers.get(name)
    if handler is None:
        return f"ERROR: unknown tool {name!r}"
    try:
        return handler()
    except KeyError as e:
        return f"ERROR: missing argument {e}"
    except Exception as e:
        return f"ERROR: {type(e).__name__}: {e}"
