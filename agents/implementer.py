"""
Implementer: delega el trabajo en Aider.

Aider ya resuelve muy bien el bucle edit-test-commit:
  - Mantiene un mapa del repo.
  - Aplica diffs de forma robusta.
  - Hace commits automáticos.
  - Soporta Ollama nativamente.

Aquí simplemente lo invocamos como subprocess con el prompt y capturamos su output.

Limitación conocida: aider-chat es interactivo por defecto. Usamos --yes-always
y --no-pretty para ejecución no interactiva.
"""
from __future__ import annotations

import subprocess
from datetime import datetime

from agents.models import AgentResult, LogEntry, TaskInput


def run(task: TaskInput) -> AgentResult:
    log: list[LogEntry] = [
        LogEntry(role=task.role, kind="info", content=f"Implementer starting on {task.feature_branch}")
    ]

    # Construir el prompt. Si venimos de una iteración previa con feedback,
    # lo incluimos al principio.
    prompt_parts = [task.prompt]
    if task.previous_feedback:
        prompt_parts.insert(0, f"[Feedback from previous review]\n{task.previous_feedback}\n\n[Task]")
    full_prompt = "\n".join(prompt_parts)

    # Modelo Ollama via aider: el formato es "ollama_chat/<model>"
    model_arg = f"ollama_chat/{task.model}"

    cmd = [
        "aider",
        "--model", model_arg,
        "--yes-always",
        "--no-pretty",
        "--no-stream",
        "--no-auto-commits",
        "--no-check-update",
        "--no-analytics",
        "--no-gitignore",
        "--no-show-release-notes",
        "--edit-format", "diff",
        "--map-tokens", "2048",
        "--message", full_prompt,
    ]
    # Aider espera que OLLAMA_API_BASE apunte al host de Ollama
    env = {
        "OLLAMA_API_BASE": task.ollama_host,
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "HOME": "/home/agent",
    }

    log.append(LogEntry(role=task.role, kind="info", content=f"Invoking: {' '.join(cmd[:6])} ..."))

    try:
        result = subprocess.run(
            cmd,
            cwd=task.worktree_path,
            capture_output=True,
            text=True,
            timeout=30 * 60,   # 30 min máximo por tarea
            env=env,
            stdin=subprocess.DEVNULL,
        )
    except subprocess.TimeoutExpired:
        return AgentResult(
            success=False,
            verdict="failed",
            summary="Aider timed out after 30 minutes",
            log=log + [LogEntry(role=task.role, kind="error", content="Timeout")],
        )

    log.append(LogEntry(role=task.role, kind="llm_message", content=result.stdout[-4000:]))
    if result.stderr:
        log.append(LogEntry(role=task.role, kind="info", content=f"stderr: {result.stderr[-2000:]}"))

    # Commit final con todo lo que Aider haya dejado en el working tree
    commit_msg = f"Implementation: {task.prompt[:72]}"
    try:
        subprocess.run(
            ["git", "add", "-A"],
            cwd=task.worktree_path,
            capture_output=True,
            text=True,
            check=True,
            timeout=30,
        )
        subprocess.run(
            ["git", "commit", "-m", commit_msg, "--allow-empty"],
            cwd=task.worktree_path,
            capture_output=True,
            text=True,
            check=True,
            timeout=30,
        )
        sha_result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=task.worktree_path,
            capture_output=True,
            text=True,
            check=True,
        )
        sha = sha_result.stdout.strip()
        commits = [sha]
        log.append(LogEntry(role=task.role, kind="info", content=f"Committed as {sha}"))
    except subprocess.CalledProcessError as e:
        commits = []
        err = (e.stderr or "").strip() or (e.stdout or "").strip() or "no output"
        log.append(LogEntry(
            role=task.role,
            kind="error",
            content=f"Commit failed ({' '.join(e.cmd)}): {err}",
        ))

    return AgentResult(
        success=result.returncode == 0,
        verdict="done" if result.returncode == 0 else "failed",
        summary=f"Aider finished with exit code {result.returncode}",
        log=log,
        commits=commits,
    )
