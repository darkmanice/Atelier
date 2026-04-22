"""Contrato de datos entre orquestador y agentes."""
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
    model: str = "gemma4:26b"
    ollama_host: str = "http://172.21.192.1:11434"


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
