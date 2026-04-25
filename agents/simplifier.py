"""
Simplifier: looks for simplifications in the files touched by the implementer
and, if any, applies and commits them.

Golden rule: it must NOT change behavior. Only form. When in doubt, leave it.
"""
from __future__ import annotations

from pathlib import Path

from agents.base import BaseAgent
from agents.models import AgentResult, TaskInput


# The role definition lives in `agents/prompts/simplifier.md`. Edit that file
# (and rebuild the agent image) to tune behavior; do not inline a copy here.
SYSTEM_PROMPT = (
    Path(__file__).parent / "prompts" / "simplifier.md"
).read_text(encoding="utf-8")


class SimplifierAgent(BaseAgent):
    def __init__(self, task: TaskInput):
        super().__init__(task, SYSTEM_PROMPT)

    def build_user_prompt(self) -> str:
        parts = [
            f"# Original task\n\n{self.task.prompt}",
            f"\n# Branches\n- base: {self.task.base_branch}\n- feature: {self.task.feature_branch}",
            "\nReview what the implementer did and simplify if you can. Start with get_diff.",
        ]
        return "\n".join(parts)


def run(task: TaskInput) -> AgentResult:
    return SimplifierAgent(task).run()
