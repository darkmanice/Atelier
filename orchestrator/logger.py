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
    path.write_text(
        f"# Task {task_id}\n\n"
        f"**Created:** {datetime.utcnow().isoformat()}Z\n"
        f"**Repo:** `{repo_path}`\n"
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
    model_reviewer: str,
    model_simplifier: str,
) -> None:
    """
    Writes the resolved LLM configuration for this task. Called once from
    the flow after `init_log`. Never includes the api_key.
    """
    # If all 3 models match, we show a single line.
    same = model_implementer == model_reviewer == model_simplifier
    if same:
        models_block = f"- **Model (3 roles):** `{model_implementer}`\n"
    else:
        models_block = (
            f"- **implementer:** `{model_implementer}`\n"
            f"- **reviewer:** `{model_reviewer}`\n"
            f"- **simplifier:** `{model_simplifier}`\n"
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
        "approved": "✅ approved",
        "changes_requested": "🔁 changes requested",
        "failed": "❌ failed",
    }.get(result.verdict or "", result.verdict or "?")

    parts = [f"### {role} — {verdict_badge}\n", f"*{_now()}*\n\n"]
    if model:
        parts.append(f"**Model:** `{model}`\n\n")

    duration = _duration_of(result.log)
    if duration is not None:
        parts.append(f"**Duration:** {duration:.1f}s\n\n")

    parts.append(f"**Summary:** {result.summary}\n\n")

    if result.commits:
        parts.append(f"**Commits:** {', '.join(f'`{c}`' for c in result.commits)}\n\n")
    if result.review_comments:
        parts.append(f"**Review comments:**\n\n```\n{result.review_comments}\n```\n\n")

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
            parts.append(f"```\n{e.content}\n```\n\n")

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
