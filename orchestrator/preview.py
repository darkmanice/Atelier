"""
Estado persistente de previews activas.

Cada preview viva tiene un sidecar JSON en logs/task-<id>.preview.json con
todo lo necesario para poder tirarla desde el worker sin tener que volver a
parsear el .pipeline-ia.yml: puerto asignado, comando de down, cwd en el host,
env vars.

Asignación de puertos: dinámica desde un rango base. Miramos qué puertos
están ocupados por otros sidecars y cogemos el primero libre. Así pueden
coexistir N previews a la vez (N = size del rango).
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from orchestrator.config import LOGS_DIR

PREVIEW_BASE_PORT = int(os.environ.get("PIPELINE_PREVIEW_BASE_PORT", "5100"))
PREVIEW_PORT_RANGE = int(os.environ.get("PIPELINE_PREVIEW_PORT_RANGE", "100"))


def sidecar_path(task_id: int) -> Path:
    return LOGS_DIR / f"task-{task_id}.preview.json"


def allocate_port() -> int:
    occupied: set[int] = set()
    if LOGS_DIR.exists():
        for p in LOGS_DIR.glob("task-*.preview.json"):
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
                port = data.get("port")
                if isinstance(port, int):
                    occupied.add(port)
            except (json.JSONDecodeError, OSError):
                continue
    for port in range(PREVIEW_BASE_PORT, PREVIEW_BASE_PORT + PREVIEW_PORT_RANGE):
        if port not in occupied:
            return port
    raise RuntimeError(
        f"No free preview port in {PREVIEW_BASE_PORT}-"
        f"{PREVIEW_BASE_PORT + PREVIEW_PORT_RANGE - 1} "
        f"({len(occupied)} previews active)"
    )


def save_sidecar(task_id: int, state: dict) -> None:
    path = sidecar_path(task_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2), encoding="utf-8")


def load_sidecar(task_id: int) -> dict | None:
    path = sidecar_path(task_id)
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def delete_sidecar(task_id: int) -> None:
    path = sidecar_path(task_id)
    if path.exists():
        path.unlink()
