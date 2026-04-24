"""Data contract between orchestrator and agents."""
from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Literal

from pydantic import BaseModel, Field


class TaskState(str, Enum):
    PENDING = "pending"
    IMPLEMENTING = "implementing"
    READY_FOR_REVIEW = "ready_for_review"
    REVIEWING = "reviewing"
    CHANGES_REQUESTED = "changes_requested"
    APPROVED = "approved"
    SIMPLIFYING = "simplifying"
    DONE = "done"
    FAILED = "failed"


class AgentRole(str, Enum):
    IMPLEMENTER = "implementer"
    REVIEWER = "reviewer"
    SIMPLIFIER = "simplifier"


class TaskInput(BaseModel):
    task_id: int
    role: AgentRole
    prompt: str
    worktree_path: str
    base_branch: str = "main"
    feature_branch: str
    previous_feedback: str | None = None

    # --- LLM config (no secrets) ---
    # provider_label is cosmetic: "nvidia", "openai", "ollama-local"...
    # base_url points to the OpenAI-compatible endpoint (with /v1 at the end).
    # The API key NEVER goes here — the worker injects it into the agent
    # container as OPENAI_API_KEY, it is never written to .task-input.json.
    provider_label: str
    base_url: str
    model: str


class LogEntry(BaseModel):
    timestamp: datetime = Field(default_factory=datetime.utcnow)
    role: AgentRole | Literal["orchestrator"]
    kind: Literal["info", "tool_call", "tool_result", "llm_message", "diff", "error"]
    content: str
    metadata: dict = Field(default_factory=dict)


class AgentResult(BaseModel):
    success: bool
    verdict: Literal["done", "approved", "changes_requested", "failed"] | None = None
    summary: str
    log: list[LogEntry] = Field(default_factory=list)
    commits: list[str] = Field(default_factory=list)
    review_comments: str | None = None
