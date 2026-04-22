"""
Esquema del fichero .pipeline-ia.yml que vive en la raíz del repo del usuario.

Cargado por el runner antes de cada fase. Todas las secciones son opcionales:
si una no está, esa fase se salta con warning.
"""
from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel, Field


class InstallConfig(BaseModel):
    command: str
    timeout: int = 300


class TestCommand(BaseModel):
    command: str
    timeout: int = 300


class E2EConfig(BaseModel):
    setup: str | None = Field(
        default=None,
        description="Comando para levantar servicios. Ejemplo: 'docker compose up -d'",
    )
    command: str
    teardown: str | None = Field(
        default=None,
        description="Comando para tirar servicios. Se ejecuta SIEMPRE, incluso si los tests fallan.",
    )
    timeout: int = 900


class PreviewConfig(BaseModel):
    """
    Preview de la tarea: un docker compose (o lo que sea) que se levanta con
    los cambios aplicados para que el usuario pueda trastearlo antes de mergear.

    El pipeline pasa PIPELINE_TASK_ID y PIPELINE_PREVIEW_PORT como variables
    de entorno al ejecutar `up` y `down`. El puerto se asigna dinámicamente del
    rango configurable (PIPELINE_PREVIEW_BASE_PORT..+range) para permitir
    varias previews simultáneas sin colisión.

    El campo `url` puede referenciar ${PIPELINE_PREVIEW_PORT} (sustitución
    string.Template) y se devuelve en /tasks/{id}/preview.
    """

    up: str = Field(description="Comando para levantar la preview.")
    down: str = Field(description="Comando para tirar la preview.")
    url: str = Field(
        default="http://localhost:${PIPELINE_PREVIEW_PORT}",
        description="URL de la preview. Soporta ${PIPELINE_PREVIEW_PORT}.",
    )
    timeout: int = 180


class PipelineConfig(BaseModel):
    """Config completo del fichero .pipeline-ia.yml."""

    install: InstallConfig | None = None
    quick_tests: TestCommand | None = None
    full_tests: TestCommand | None = None
    e2e_tests: E2EConfig | None = None
    preview: PreviewConfig | None = None


CONFIG_FILENAME = ".pipeline-ia.yml"


def load_config(worktree_path: Path) -> PipelineConfig | None:
    """
    Carga el config del worktree.

    Returns:
        PipelineConfig si existe y es válido.
        None si el fichero no existe (caller debe emitir warning).

    Raises:
        ValueError si el fichero existe pero es inválido.
    """
    config_file = worktree_path / CONFIG_FILENAME
    if not config_file.exists():
        return None

    try:
        raw = yaml.safe_load(config_file.read_text(encoding="utf-8"))
    except yaml.YAMLError as e:
        raise ValueError(f"{CONFIG_FILENAME} is not valid YAML: {e}") from e

    if not isinstance(raw, dict):
        raise ValueError(f"{CONFIG_FILENAME} must be a YAML object at top level")

    return PipelineConfig(**raw)
