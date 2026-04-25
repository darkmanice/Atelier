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
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from agents.models import AgentResult, LogEntry, TaskInput


# The role definition (including the SEARCH/REPLACE format example aider needs)
# lives in `agents/prompts/implementer.md`. The file is loaded once at import
# time and prepended to the user message that aider sends to the LLM. Edit the
# .md (and rebuild the agent image) to tune behavior.
_PROMPT_PREAMBLE = (
    Path(__file__).parent / "prompts" / "implementer.md"
).read_text(encoding="utf-8")


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

    # Build the prompt: role definition (loaded from agents/prompts/implementer.md)
    # + the actual task. Previous feedback, if any, goes at the very top so the
    # model reads it before the role.
    prompt_parts = [f"{_PROMPT_PREAMBLE}\n\n[Task]\n{task.prompt}"]
    if task.previous_feedback:
        prompt_parts.insert(0, f"[Feedback from previous review]\n{task.previous_feedback}\n")
    full_prompt = "\n".join(prompt_parts)

    # Model via LiteLLM (used by Aider): the `openai/` prefix makes LiteLLM
    # respect OPENAI_API_BASE and OPENAI_API_KEY from the environment, which
    # is how the worker injects the endpoint and the key into the agent
    # container. Works against any OpenAI-compatible endpoint (NVIDIA, OpenAI,
    # OpenRouter, vLLM, Ollama via /v1 ...).
    model_arg = f"openai/{task.model}"

    # Edit format is configurable so a project can opt into a more forgiving
    # output style with weaker models. `diff` (default) = SEARCH/REPLACE
    # blocks, minimal but format-strict. `whole` = full-file rewrites,
    # forgiving but token-expensive. `udiff` = unified diff, middle ground.
    edit_format = os.environ.get("AIDER_EDIT_FORMAT", "diff").strip() or "diff"

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
        "--edit-format", edit_format,
        "--map-tokens", "2048",
        # Suppress Aider's chat-history summarizer. With `--message` (one-shot)
        # Aider exits right after the edit and its background summarizer
        # thread pool is shut down before it can finish, which spams
        # "cannot schedule new futures after shutdown" into stdout. Setting
        # the history cap absurdly high means the summarizer never triggers.
        "--max-chat-history-tokens", "999999999",
        # Redirect Aider's history files out of the worktree so they do not
        # end up in the commit. The tags cache (.aider.tags.cache.v4/) has
        # no flag to relocate; we wipe it below before the commit.
        "--chat-history-file", "/tmp/aider-chat.md",
        "--input-history-file", "/tmp/aider-input.history",
        "--llm-history-file", "/tmp/aider-llm.history",
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

    # Sweep any Aider artefacts (tags cache dir, stray history files the
    # flags above did not catch, etc.) so `git add -A` below never picks
    # them up. The user does not want these in their feature-branch commits.
    _purge_aider_artifacts(Path(task.worktree_path), log, task.role)

    # Branch sandbox: if Aider (or anything else) left HEAD on a branch other
    # than the task branch, refuse to commit. The ref snapshot on the worker
    # side is still the main guardrail; this is the fast-fail on our side.
    commits: list[str] = []
    try:
        current_branch = subprocess.run(
            ["git", "symbolic-ref", "--short", "HEAD"],
            cwd=task.worktree_path,
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
        ).stdout.strip()
    except subprocess.CalledProcessError:
        current_branch = ""

    if current_branch != task.feature_branch:
        log.append(LogEntry(
            role=task.role,
            kind="error",
            content=(
                f"Refusing to commit: HEAD is on '{current_branch or '(detached)'}', "
                f"task branch is '{task.feature_branch}'."
            ),
        ))
        return AgentResult(
            success=False,
            verdict="failed",
            summary=f"Refusing to commit: HEAD is on '{current_branch or '(detached)'}', task branch is '{task.feature_branch}'.",
            log=log,
            commits=[],
        )

    # Aider exit 0 does NOT mean it actually applied any edit — when the
    # model emits malformed SEARCH/REPLACE blocks, aider rejects them all
    # and exits cleanly with an empty working tree. Catch that here so the
    # reviewer doesn't waste 10 minutes spinning over `(no changes)`.
    try:
        status_result = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=task.worktree_path,
            capture_output=True,
            text=True,
            check=True,
            timeout=30,
        )
        working_tree_dirty = bool((status_result.stdout or "").strip())
    except subprocess.CalledProcessError as e:
        log.append(LogEntry(
            role=task.role,
            kind="error",
            content=f"git status failed: {(e.stderr or e.stdout or '').strip()}",
        ))
        return AgentResult(
            success=False, verdict="failed",
            summary=f"git status failed after aider: {(e.stderr or '').strip()[:200]}",
            log=log, commits=[],
        )

    if not working_tree_dirty:
        summary = (
            "Aider exited 0 but produced no file changes. The model likely "
            "emitted invalid SEARCH/REPLACE blocks (aider rejected them all). "
            "Try a coder-tuned model such as qwen2.5-coder:32b."
        )
        log.append(LogEntry(role=task.role, kind="error", content=summary))
        return AgentResult(
            success=False,
            verdict="failed",
            summary=summary,
            log=log,
            commits=[],
        )

    # Final commit with everything Aider has left in the working tree.
    # No `--allow-empty`: we already verified the working tree is dirty.
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
            ["git", "commit", "-m", commit_msg],
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
        err = (e.stderr or "").strip() or (e.stdout or "").strip() or "no output"
        log.append(LogEntry(
            role=task.role,
            kind="error",
            content=f"Commit failed ({' '.join(e.cmd)}): {err}",
        ))
        return AgentResult(
            success=False,
            verdict="failed",
            summary=f"Commit failed: {err[:300]}",
            log=log,
            commits=[],
        )

    return AgentResult(
        success=returncode == 0,
        verdict="done" if returncode == 0 else "failed",
        summary=_summarize_aider(stdout, returncode),
        log=log,
        commits=commits,
    )


def _purge_aider_artifacts(worktree: Path, log: list[LogEntry], role) -> None:
    """Remove any .aider* file or directory left in the worktree.

    Aider drops several files into the project root (chat history, input
    history, tags cache). We redirect most via CLI flags but the tags cache
    has no equivalent flag, and older/new Aider versions may add more. Wipe
    anything matching `.aider*` before the implementer commits so no Aider
    bookkeeping leaks into the task branch.
    """
    removed: list[str] = []
    for entry in worktree.glob(".aider*"):
        try:
            if entry.is_dir():
                shutil.rmtree(entry, ignore_errors=True)
            else:
                entry.unlink(missing_ok=True)
            removed.append(entry.name)
        except OSError:
            pass
    if removed:
        log.append(LogEntry(
            role=role, kind="info",
            content=f"Cleaned up Aider artifacts: {', '.join(removed)}",
        ))


_AIDER_NOISE_PREFIXES = (
    "<<<<<<< ", ">>>>>>> ", "======",   # REPLACE block markers
)
_AIDER_KEEP_KEYWORDS = (
    "applied edit",
    "commit ",
    "tokens:",
    "summarization failed",
    "summarizer unexpectedly",
    "aider ok",
    "aider error",
    "no changes",
    "exit code",
)


def _summarize_aider(stdout: str, returncode: int) -> str:
    """
    Extract a short explanation of what Aider did from its stdout.

    Aider echoes the full REPLACE blocks, chat prose, and status lines into
    stdout. For the task summary we only want the status-ish lines
    ("Applied edit to ...", "Commit abc1234 ...", "Tokens: ...", etc.).
    Code dumps between ``` fences and REPLACE conflict markers are filtered
    out so the summary stays short and readable.
    """
    kept: list[str] = []
    in_fence = False
    for raw in stdout.splitlines():
        line = raw.rstrip()
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        if stripped.startswith(_AIDER_NOISE_PREFIXES):
            continue
        # Keep only lines that look like status output; skip prose/code leaks.
        low = stripped.lower()
        if any(kw in low for kw in _AIDER_KEEP_KEYWORDS):
            kept.append(stripped)

    status = "ok" if returncode == 0 else f"exit {returncode}"
    if not kept:
        return f"Aider {status} (no status lines captured)"
    return f"Aider {status}: " + " · ".join(kept[-6:])
