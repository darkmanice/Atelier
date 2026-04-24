"""
Schema for the .atelier.yml file that lives at the root of the user's repo.

Loaded by the runner before each phase. All sections are optional: if one is
missing, that phase is skipped with a warning.
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
        description="Command to bring up services. Example: 'docker compose up -d'",
    )
    command: str
    teardown: str | None = Field(
        default=None,
        description="Command to tear down services. Runs ALWAYS, even if tests fail.",
    )
    timeout: int = 900


class PreviewConfig(BaseModel):
    """
    Task preview: a docker compose (or whatever) that is brought up with the
    applied changes so the user can play with it before merging.

    The pipeline passes TASK_ID and PREVIEW_PORT as environment variables
    when running `up` and `down`. The port is assigned dynamically from the
    configurable range (PREVIEW_BASE_PORT..+range) to allow several
    simultaneous previews without collision.

    The `url` field can reference ${PREVIEW_PORT} (string.Template
    substitution) and is returned in /tasks/{id}/preview.
    """

    up: str = Field(description="Command to bring up the preview.")
    down: str = Field(description="Command to tear down the preview.")
    url: str = Field(
        default="http://localhost:${PREVIEW_PORT}",
        description="Preview URL. Supports ${PREVIEW_PORT}.",
    )
    timeout: int = 180


class PipelineConfig(BaseModel):
    """Full config of the .atelier.yml file."""

    install: InstallConfig | None = None
    quick_tests: TestCommand | None = None
    full_tests: TestCommand | None = None
    e2e_tests: E2EConfig | None = None
    preview: PreviewConfig | None = None


CONFIG_FILENAME = ".atelier.yml"


def load_config(worktree_path: Path) -> PipelineConfig | None:
    """
    Loads the worktree's config.

    Returns:
        PipelineConfig if it exists and is valid.
        None if the file does not exist (caller must emit a warning).

    Raises:
        ValueError if the file exists but is invalid.
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
