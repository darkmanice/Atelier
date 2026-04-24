"""
Implementer: delegates the work to Aider.

Aider already solves the edit-test-commit loop very well:
  - Maintains a repo map.
  - Applies diffs robustly.
  - Makes automatic commits.
  - Supports Ollama natively.

Here we simply invoke it as a subprocess with the prompt and capture its output.

Known limitation: aider-chat is interactive by default. We use --yes-always
and --no-pretty for non-interactive execution.
"""
from __future__ import annotations

import os
import subprocess
import sys
from datetime import datetime

from agents.models import AgentResult, LogEntry, TaskInput


def _echo(msg: str) -> None:
    """
    Writes a line to the container's stderr so `docker logs -f` sees it live.
    We use stderr because stdout is reserved for the final AgentResult JSON
    that the worker parses with _parse_agent_output.
    """
    print(f"[implementer] {msg}", file=sys.stderr, flush=True)


def run(task: TaskInput) -> AgentResult:
    log: list[LogEntry] = [
        LogEntry(role=task.role, kind="info", content=f"Implementer starting on {task.feature_branch}")
    ]

    # Build the prompt. If we come from a previous iteration with feedback,
    # include it at the beginning.
    prompt_parts = [task.prompt]
    if task.previous_feedback:
        prompt_parts.insert(0, f"[Feedback from previous review]\n{task.previous_feedback}\n\n[Task]")
    full_prompt = "\n".join(prompt_parts)

    # Model via LiteLLM (used by Aider): the `openai/` prefix makes LiteLLM
    # respect OPENAI_API_BASE and OPENAI_API_KEY from the environment, which
    # is how the worker injects the endpoint and the key into the agent
    # container. Works against any OpenAI-compatible endpoint (NVIDIA, OpenAI,
    # OpenRouter, vLLM, Ollama via /v1 ...).
    model_arg = f"openai/{task.model}"

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
    # Pass Aider the same endpoint and key that are already in our environment
    # (injected by the worker). PYTHONUNBUFFERED ensures that aider/litellm do
    # not stay in a Python buffer; we want to see lines as soon as they are
    # produced, so `docker logs -f` is useful.
    env = {
        "OPENAI_API_BASE": os.environ.get("OPENAI_API_BASE", ""),
        "OPENAI_API_KEY": os.environ.get("OPENAI_API_KEY", ""),
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "HOME": "/home/agent",
        "PYTHONUNBUFFERED": "1",
    }

    _echo(f"starting on branch {task.feature_branch}, model={task.model}")
    _echo(f"invoking aider: {' '.join(cmd[:6])} ...")
    log.append(LogEntry(role=task.role, kind="info", content=f"Invoking: {' '.join(cmd[:6])} ..."))

    # Popen + manual streaming: we forward each line to the container's stderr
    # so `docker logs -f` shows aider live, and store them for the later
    # summary. If we used subprocess.run(capture_output=True) we would get
    # total silence until aider finishes.
    try:
        proc = subprocess.Popen(
            cmd,
            cwd=task.worktree_path,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,   # merge stderr with stdout
            text=True,
            bufsize=1,                  # line-buffered
            env=env,
            stdin=subprocess.DEVNULL,
        )
    except FileNotFoundError as e:
        return AgentResult(
            success=False, verdict="failed",
            summary=f"Could not start aider: {e}",
            log=log + [LogEntry(role=task.role, kind="error", content=str(e))],
        )

    captured_lines: list[str] = []
    try:
        assert proc.stdout is not None
        for line in proc.stdout:
            sys.stderr.write(line)
            sys.stderr.flush()
            captured_lines.append(line)
        returncode = proc.wait(timeout=30 * 60)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
        _echo("timed out after 30 minutes, killed")
        return AgentResult(
            success=False,
            verdict="failed",
            summary="Aider timed out after 30 minutes",
            log=log + [LogEntry(role=task.role, kind="error", content="Timeout")],
        )

    stdout = "".join(captured_lines)
    _echo(f"aider finished with exit code {returncode}")

    if len(stdout) > 6000:
        captured = stdout[:2000] + "\n\n...[aider stdout truncated in the middle]...\n\n" + stdout[-4000:]
    else:
        captured = stdout
    log.append(LogEntry(role=task.role, kind="llm_message", content=captured))

    # Final commit with everything Aider has left in the working tree
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
        success=returncode == 0,
        verdict="done" if returncode == 0 else "failed",
        summary=_summarize_aider(stdout, returncode),
        log=log,
        commits=commits,
    )


def _summarize_aider(stdout: str, returncode: int) -> str:
    """
    Extract a short explanation of what Aider did from its stdout.

    Aider ends with lines like:
        Applied edit to path/to/file.py
        Commit abc1234 feat: ...
    We take the last non-empty lines as a readable summary, instead of just
    "exit code X" which tells the user nothing.
    """
    meaningful = [line for line in stdout.splitlines() if line.strip()]
    if not meaningful:
        return f"Aider finished with exit code {returncode} (no output)"

    tail = meaningful[-8:]
    tail_text = "\n".join(tail).strip()
    status = "ok" if returncode == 0 else f"exit {returncode}"
    return f"Aider {status}:\n{tail_text}"
