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


class PipelineConfig(BaseModel):
    """Config completo del fichero .pipeline-ia.yml."""

    install: InstallConfig | None = None
    quick_tests: TestCommand | None = None
    full_tests: TestCommand | None = None
    e2e_tests: E2EConfig | None = None


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
