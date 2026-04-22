"""
Entrypoint del contenedor. Lee TaskInput de /workspace/.task-input.json,
decide qué rol correr, emite AgentResult por stdout.
"""
from __future__ import annotations

import json
import os
import sys
import traceback
from pathlib import Path

from agents.models import AgentResult, AgentRole, LogEntry, TaskInput


INPUT_FILE = Path(os.environ.get("TASK_INPUT_FILE", "/workspace/.task-input.json"))


def main() -> int:
    # 1. Leer TaskInput del fichero
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

    # 2. Borrar el fichero de input para no contaminar commits posteriores
    try:
        INPUT_FILE.unlink()
    except Exception:
        pass

    # 3. Despachar al agente correcto
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
