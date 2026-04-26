"""
OpenHands V1 SDK wrapper.

Replaces the V0 multi-role agents (aider-based implementer +
BaseAgent-based reviewer/simplifier) with a single autonomous loop
provided by the SDK. The wrapper:

  - Builds an `LLM` pointing to the user's provider (model + base_url +
    api_key) — works against any OpenAI-compatible endpoint, including
    a local Ollama at `http://<wsl-gateway>:11434/v1`. The model MUST
    support tool-calling.
  - Equips the Agent with terminal + file_editor + task_tracker tools
    from `openhands-tools`.
  - Runs a `Conversation` against `task.worktree_path` as the
    workspace. Commands the agent issues run as subprocesses ON THE
    WORKER HOST (not in a Docker sandbox) — atelier's `branch_guard`
    is what keeps the agent on its feature branch.
  - Subscribes a callback that streams every `Event` to
    `<worktree>/.task-events.jsonl` so the orchestrator can later
    move it to `logs/task-<id>.events.jsonl` (P3: events persisted
    but not rendered in the UI yet).
  - After `conversation.run()` returns, does the manual
    `git add -A && git commit` so every attempt produces a commit
    (matches the existing aider-based implementer's behavior).
"""
from __future__ import annotations

import os
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Literal

from pydantic import SecretStr

from agents.models import AgentResult, LogEntry, TaskInput


_PROMPTS_DIR = Path(__file__).parent / "prompts"
_PROMPT_BY_MODE: dict[str, Path] = {
    "implement": _PROMPTS_DIR / "openhands_implementer.md",
    "simplify": _PROMPTS_DIR / "openhands_simplifier.md",
}


# How many agent steps a single OpenHands run is allowed to take. Each
# step is roughly one LLM call + one tool execution. With a slow local
# model this caps wall-clock time. Tunable per env without rebuilds.
_MAX_ITERATIONS = int(os.environ.get("OPENHANDS_MAX_ITERATIONS", "30"))


SessionMode = Literal["implement", "simplify"]


def _echo(msg: str) -> None:
    """Forward a status line to the worker's stderr so it shows up in
    `docker compose logs -f prefect-worker`."""
    print(f"[openhands] {msg}", file=sys.stderr, flush=True)


def run(task: TaskInput, mode: SessionMode, api_key: str = "") -> AgentResult:
    """Run a single OpenHands session in `task.worktree_path`.

    `mode` selects the system prompt and the post-run policy:
      - `"implement"` requires the working tree to be dirty when the
        run finishes (an empty diff is treated as a failure, same as
        the old aider implementer).
      - `"simplify"` allows a no-op (an empty diff is OK and yields a
        success without a commit).

    `api_key` is passed in here (not on TaskInput) for the same reason
    aider used to receive it as a sibling argument: it must never be
    serialised to disk via TaskInput / .task-events.jsonl / Prefect
    parameters.
    """
    log: list[LogEntry] = [
        LogEntry(role=task.role, kind="info",
                 content=f"OpenHands {mode} starting on {task.feature_branch}"),
    ]

    prompt_file = _PROMPT_BY_MODE.get(mode)
    if prompt_file is None or not prompt_file.exists():
        return AgentResult(
            success=False, verdict="failed",
            summary=f"Unknown OpenHands mode: {mode!r}",
            log=log + [LogEntry(role=task.role, kind="error",
                                content=f"No prompt file for mode {mode!r}")],
        )
    role_prompt = prompt_file.read_text(encoding="utf-8")

    # Compose the message: role definition + task + (optional) feedback.
    # Feedback goes first so the model reads it before the role prompt
    # and treats the iteration as a correction pass.
    message_parts: list[str] = []
    if task.previous_feedback:
        message_parts.append(
            f"[Feedback from previous attempt]\n{task.previous_feedback}\n"
        )
    message_parts.append(role_prompt)
    message_parts.append(f"\n[Task]\n{task.prompt}")
    full_message = "\n".join(message_parts)

    worktree = Path(task.worktree_path)
    events_file = worktree / ".task-events.jsonl"
    # Wipe any leftover from a previous attempt so callers reading the
    # file get a clean stream for this run.
    events_file.unlink(missing_ok=True)

    # Imports are local: openhands-sdk pulls in a heavy dep tree at
    # import time (litellm, anthropic, browsergym, ...). Importing only
    # when the wrapper is actually invoked keeps Prefect flow loading
    # snappy when no agent task is in flight.
    try:
        from openhands.sdk import (
            LLM,
            Agent,
            Conversation,
            Event,
        )
        from openhands.sdk.tool import Tool
        from openhands.tools.file_editor import FileEditorTool
        from openhands.tools.task_tracker import TaskTrackerTool
        from openhands.tools.terminal import TerminalTool
    except ImportError as e:
        return AgentResult(
            success=False, verdict="failed",
            summary=f"openhands-sdk not installed in the worker image: {e}",
            log=log + [LogEntry(role=task.role, kind="error", content=str(e))],
        )

    api_key_value = api_key or "sk-no-auth"
    base_url = task.base_url

    # `openai/<name>` makes LiteLLM (used by the SDK) treat the endpoint
    # as OpenAI-compatible regardless of the actual provider. This is
    # what lets us point at Ollama, vLLM, NVIDIA, etc. with the same
    # code path.
    model_id = task.model if "/" in task.model else f"openai/{task.model}"

    llm = LLM(
        usage_id=f"atelier-task-{task.task_id}-{mode}",
        model=model_id,
        base_url=base_url or None,
        api_key=SecretStr(api_key_value),
    )

    agent = Agent(
        llm=llm,
        tools=[
            Tool(name=TerminalTool.name),
            Tool(name=FileEditorTool.name),
            Tool(name=TaskTrackerTool.name),
        ],
    )

    def _persist_event(event: "Event") -> None:
        # Append-only JSONL: one event per line. Open/close on each
        # call so a crash mid-run still leaves a valid prefix on disk.
        try:
            payload = event.model_dump_json()
        except Exception as exc:
            payload = f'{{"kind":"unserializable","error":{exc!r}}}'
        try:
            with events_file.open("a", encoding="utf-8") as f:
                f.write(payload + "\n")
        except OSError:
            # Best-effort persistence: never let logging break the run.
            pass

    conversation = Conversation(
        agent=agent,
        callbacks=[_persist_event],
        workspace=str(worktree),
        conversation_id=uuid.uuid4(),
        max_iterations=_MAX_ITERATIONS,
    )

    _echo(f"running mode={mode} model={model_id} max_iter={_MAX_ITERATIONS}")
    log.append(LogEntry(
        role=task.role, kind="info",
        content=f"Invoking OpenHands ({mode}) with model={model_id}, max_iterations={_MAX_ITERATIONS}",
    ))

    try:
        conversation.send_message(full_message)
        conversation.run()
    except Exception as exc:
        _echo(f"OpenHands raised: {type(exc).__name__}: {exc}")
        return AgentResult(
            success=False, verdict="failed",
            summary=f"OpenHands raised {type(exc).__name__}: {exc}"[:300],
            log=log + [LogEntry(role=task.role, kind="error",
                                content=f"{type(exc).__name__}: {exc}")],
        )

    return _finalize(task=task, mode=mode, log=log, worktree=worktree)


def _finalize(
    *,
    task: TaskInput,
    mode: SessionMode,
    log: list[LogEntry],
    worktree: Path,
) -> AgentResult:
    """Branch sandbox check + dirty check + commit. Mirrors what the
    aider-based implementer used to do, parametrized by mode."""

    # Branch sandbox: if anything moved HEAD off the feature branch,
    # refuse to commit. This is a defensive check on top of the worker-
    # side ref snapshot guard.
    try:
        current_branch = subprocess.run(
            ["git", "symbolic-ref", "--short", "HEAD"],
            cwd=worktree, capture_output=True, text=True,
            check=True, timeout=10,
        ).stdout.strip()
    except subprocess.CalledProcessError:
        current_branch = ""

    if current_branch != task.feature_branch:
        msg = (
            f"Refusing to commit: HEAD is on '{current_branch or '(detached)'}', "
            f"task branch is '{task.feature_branch}'."
        )
        log.append(LogEntry(role=task.role, kind="error", content=msg))
        return AgentResult(
            success=False, verdict="failed", summary=msg,
            log=log, commits=[],
        )

    try:
        status = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=worktree, capture_output=True, text=True,
            check=True, timeout=30,
        ).stdout
    except subprocess.CalledProcessError as exc:
        err = (exc.stderr or exc.stdout or "").strip()
        log.append(LogEntry(role=task.role, kind="error",
                            content=f"git status failed: {err}"))
        return AgentResult(
            success=False, verdict="failed",
            summary=f"git status failed after OpenHands: {err[:200]}",
            log=log, commits=[],
        )
    working_tree_dirty = bool(status.strip())

    if not working_tree_dirty:
        if mode == "implement":
            msg = (
                "OpenHands finished without modifying any file. The model "
                "likely refused or got stuck. Try a stronger coder-tuned "
                "model or check the event log at "
                f"{worktree}/.task-events.jsonl."
            )
            log.append(LogEntry(role=task.role, kind="error", content=msg))
            return AgentResult(
                success=False, verdict="failed", summary=msg,
                log=log, commits=[],
            )
        # Simplify is allowed to be a no-op.
        log.append(LogEntry(role=task.role, kind="info",
                            content="No changes to simplify; nothing to commit."))
        return AgentResult(
            success=True, verdict="done",
            summary="OpenHands simplify: no changes.",
            log=log, commits=[],
        )

    # Commit everything in the working tree.
    commit_msg = f"{mode.capitalize()}: {task.prompt[:72]}"
    try:
        subprocess.run(
            ["git", "add", "-A"], cwd=worktree,
            capture_output=True, text=True, check=True, timeout=30,
        )
        subprocess.run(
            ["git", "commit", "-m", commit_msg], cwd=worktree,
            capture_output=True, text=True, check=True, timeout=30,
        )
        sha = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"], cwd=worktree,
            capture_output=True, text=True, check=True,
        ).stdout.strip()
    except subprocess.CalledProcessError as exc:
        err = (exc.stderr or exc.stdout or "").strip() or "no output"
        log.append(LogEntry(
            role=task.role, kind="error",
            content=f"Commit failed ({' '.join(exc.cmd)}): {err}",
        ))
        return AgentResult(
            success=False, verdict="failed",
            summary=f"Commit failed: {err[:300]}",
            log=log, commits=[],
        )

    log.append(LogEntry(role=task.role, kind="info",
                        content=f"Committed as {sha}"))
    return AgentResult(
        success=True, verdict="done",
        summary=f"OpenHands {mode}: committed {sha}",
        log=log, commits=[sha],
    )
