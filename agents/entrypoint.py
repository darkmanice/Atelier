"""
Container entrypoint. Reads TaskInput from the path the worker injected
via `TASK_INPUT_FILE` (always set), decides which role to run, emits
AgentResult via stdout.
"""
from __future__ import annotations

import json
import os
import sys
import traceback
from pathlib import Path

from agents.models import AgentResult, AgentRole, LogEntry, TaskInput


INPUT_FILE = Path(os.environ["TASK_INPUT_FILE"])


def main() -> int:
    # 1. Read TaskInput from the file
    try:
        if not INPUT_FILE.exists():
            raise FileNotFoundError(f"{INPUT_FILE} not found")
        raw = INPUT_FILE.read_text(encoding="utf-8")
        task = TaskInput.model_validate_json(raw)
    except Exception as e:
        err = AgentResult(
            success=False,
            verdict="failed",
            summary=f"Invalid TaskInput: {e}",
            log=[LogEntry(role="orchestrator", kind="error", content=str(e))],
        )
        print(err.model_dump_json())
        return 2

    # 2. Delete the input file so it does not contaminate later commits
    try:
        INPUT_FILE.unlink()
    except Exception:
        pass

    # 3. Dispatch to the correct agent
    try:
        if task.role == AgentRole.IMPLEMENTER:
            from agents.implementer import run as run_impl
            result = run_impl(task)
        elif task.role == AgentRole.REVIEWER:
            from agents.reviewer import run as run_review
            result = run_review(task)
        elif task.role == AgentRole.SIMPLIFIER:
            from agents.simplifier import run as run_simplify
            result = run_simplify(task)
        else:
            raise ValueError(f"Unknown role: {task.role}")
    except Exception as e:
        tb = traceback.format_exc()
        result = AgentResult(
            success=False,
            verdict="failed",
            summary=f"Unhandled exception in {task.role}: {e}",
            log=[LogEntry(role=task.role, kind="error", content=tb)],
        )

    print(result.model_dump_json())
    return 0 if result.success else 1


if __name__ == "__main__":
    sys.exit(main())
