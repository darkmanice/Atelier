"""Markdown log per task."""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

from agents.models import AgentResult, LogEntry
from orchestrator.config import LOGS_DIR


def log_path(task_id: int) -> Path:
    return LOGS_DIR / f"task-{task_id}.md"


def init_log(task_id: int, prompt: str, repo_path: str, feature_branch: str) -> None:
    path = log_path(task_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Show only the project's basename; the /projects/ root is implicit.
    project = Path(repo_path).name or repo_path
    path.write_text(
        f"# Task {task_id}\n\n"
        f"**Created:** {datetime.utcnow().isoformat()}Z\n"
        f"**Project:** `{project}`\n"
        f"**Branch:** `{feature_branch}`\n\n"
        f"## Prompt\n\n{prompt}\n\n"
        f"---\n\n",
        encoding="utf-8",
    )


def append_llm_config(
    task_id: int,
    provider_label: str,
    base_url: str,
    model_implementer: str,
    model_simplifier: str,
) -> None:
    """
    Writes the resolved LLM configuration for this task. Called once from
    the flow after `init_log`. Never includes the api_key.

    `model_implementer` drives the `agent-session` block; `model_simplifier`
    drives `simplify-pass` (when present in the spec).
    """
    if model_implementer == model_simplifier:
        models_block = f"- **Model:** `{model_implementer}`\n"
    else:
        models_block = (
            f"- **agent-session:** `{model_implementer}`\n"
            f"- **simplify-pass:** `{model_simplifier}`\n"
        )
    _append(
        task_id,
        "## LLM configuration\n\n"
        f"- **Provider:** `{provider_label}`\n"
        f"- **Base URL:** `{base_url}`\n"
        f"{models_block}\n"
        "---\n\n"
        "## Timeline\n\n",
    )


def append_orchestrator(task_id: int, message: str) -> None:
    _append(task_id, f"> **orchestrator** · {_now()} · {message}\n\n")


def append_agent_result(
    task_id: int,
    role: str,
    result: AgentResult,
    model: str | None = None,
) -> None:
    verdict_badge = {
        "done": "✅ done",
        "failed": "❌ failed",
    }.get(result.verdict or "", result.verdict or "?")

    parts = [f"### {role} — {verdict_badge}\n", f"*{_now()}*\n\n"]
    if model:
        parts.append(f"**Model:** `{model}`\n\n")

    duration = _duration_of(result.log)
    if duration is not None:
        parts.append(f"**Duration:** {duration:.1f}s\n\n")

    parts.append(_format_summary(result.summary))

    if result.commits:
        parts.append(f"**Commits:** {', '.join(f'`{c}`' for c in result.commits)}\n\n")

    tool_calls = [e for e in result.log if e.kind in ("tool_call", "tool_result")]
    if tool_calls:
        call_count = sum(1 for e in tool_calls if e.kind == "tool_call")
        parts.append(f"<details><summary>Tool calls ({call_count})</summary>\n\n")
        for entry in tool_calls:
            prefix = "→" if entry.kind == "tool_call" else "←"
            content = entry.content[:800] + ("…" if len(entry.content) > 800 else "")
            parts.append(f"- `{prefix}` {content}\n")
        parts.append("\n</details>\n\n")

    errors = [e for e in result.log if e.kind == "error"]
    if errors:
        parts.append("**Errors:**\n\n")
        for e in errors:
            parts.append(_safe_fenced_block(e.content) + "\n\n")

    parts.append("---\n\n")
    _append(task_id, "".join(parts))


def append_final(task_id: int, final_state: str, summary: str, diff_stat: str) -> None:
    _append(
        task_id,
        f"## Final\n\n"
        f"**State:** {final_state}\n\n"
        f"**Summary:** {summary}\n\n"
        f"**Diff stat:**\n\n```\n{diff_stat}\n```\n",
    )


def _duration_of(entries: list[LogEntry]) -> float | None:
    """Difference between the first and last timestamp of the agent log."""
    if len(entries) < 2:
        return None
    try:
        first = entries[0].timestamp
        last = entries[-1].timestamp
    except AttributeError:
        return None
    return (last - first).total_seconds()


def _append(task_id: int, text: str) -> None:
    path = log_path(task_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(text)


def _now() -> str:
    return datetime.utcnow().strftime("%H:%M:%S")


def _safe_fenced_block(text: str) -> str:
    """Wrap arbitrary text in a fenced code block, handling embedded fences.

    Markdown closes a ``` fence on the next ``` it sees. If the text itself
    contains triple backticks (very common with Aider output, which echoes
    REPLACE blocks), the block bleeds into the rest of the document. Use
    enough backticks on the outside to dominate whatever is inside.
    """
    text = (text or "").rstrip()
    # Pick a fence of 3+ backticks longer than the longest run inside.
    longest_run = 0
    run = 0
    for ch in text:
        if ch == "`":
            run += 1
            longest_run = max(longest_run, run)
        else:
            run = 0
    fence = "`" * max(3, longest_run + 1)
    return f"{fence}\n{text}\n{fence}"


def _format_summary(summary: str) -> str:
    """Render a summary value for the task markdown.

    Single-line summaries go inline next to the **Summary:** label. Multi-line
    or fence-heavy summaries drop into a standalone fenced block below, with
    enough backticks to dominate any fences inside (Aider REPLACE dumps
    commonly carry embedded ```).
    """
    summary = (summary or "").strip()
    if not summary:
        return "**Summary:** _(empty)_\n\n"
    if "\n" not in summary and "```" not in summary:
        return f"**Summary:** {summary}\n\n"
    return "**Summary:**\n\n" + _safe_fenced_block(summary) + "\n\n"
