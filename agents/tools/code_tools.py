"""
Tools que el reviewer y el simplifier pueden invocar.

El implementer NO usa este módulo porque delega en Aider, que ya trae su propio
set de herramientas más sofisticado (con RAG sobre el repo, aplicación de patches, etc.)

Las definiciones siguen el formato de tool-calling de Ollama (compatible OpenAI).
Cada función devuelve un string (lo que el agente verá en el siguiente turno).
"""
from __future__ import annotations

import subprocess
from pathlib import Path

MAX_FILE_CHARS = 20_000          # corta ficheros enormes al leer
MAX_DIFF_CHARS = 60_000          # corta diffs enormes
MAX_LIST_FILES = 200             # corta listings


# -----------------------
# Definiciones (schema)
# -----------------------

TOOL_DEFINITIONS = [
    {
        "type": "function",
        "function": {
            "name": "list_files",
            "description": "Lista los ficheros del worktree. Útil para orientarse al empezar.",
            "parameters": {
                "type": "object",
                "properties": {
                    "subpath": {
                        "type": "string",
                        "description": "Subruta relativa al worktree. Vacío = raíz.",
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
            "description": "Lee el contenido de un fichero del worktree.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Ruta relativa al worktree.",
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
            "description": "Devuelve el diff entre la rama actual y la rama base. Útil para review.",
            "parameters": {
                "type": "object",
                "properties": {
                    "base_branch": {"type": "string", "description": "Rama base (ej: 'main')."},
                },
                "required": ["base_branch"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "Sobrescribe un fichero. Solo usar en rol simplifier. Crea directorios si hace falta.",
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
            "description": "Hace commit de los cambios actuales con el mensaje dado.",
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
                "Termina la tarea. "
                "Para reviewer: verdict='approved' o 'changes_requested' + comments. "
                "Para simplifier: verdict='done' + summary."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "verdict": {
                        "type": "string",
                        "enum": ["approved", "changes_requested", "done"],
                    },
                    "summary": {"type": "string"},
                    "comments": {"type": "string", "description": "Feedback detallado."},
                },
                "required": ["verdict", "summary"],
            },
        },
    },
]


# -----------------------
# Implementaciones
# -----------------------


def _safe_path(worktree: Path, relative: str) -> Path:
    """Evita escapes del worktree con paths relativos maliciosos."""
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


def git_commit(worktree: Path, message: str) -> str:
    try:
        subprocess.run(["git", "add", "-A"], cwd=worktree, check=True, timeout=30)
        result = subprocess.run(
            ["git", "commit", "-m", message, "--allow-empty"],
            cwd=worktree,
            capture_output=True,
            text=True,
            timeout=30,
            check=True,
        )
        # devolver el hash corto del commit
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


# Dispatcher que el loop ReAct invoca
def dispatch(name: str, arguments: dict, worktree: Path) -> str:
    handlers = {
        "list_files": lambda: list_files(worktree, arguments.get("subpath", "")),
        "read_file": lambda: read_file(worktree, arguments["path"]),
        "get_diff": lambda: get_diff(worktree, arguments["base_branch"]),
        "write_file": lambda: write_file(worktree, arguments["path"], arguments["content"]),
        "git_commit": lambda: git_commit(worktree, arguments["message"]),
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
