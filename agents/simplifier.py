"""
Simplifier: looks for simplifications in the files touched by the implementer
and, if any, applies and commits them.

Golden rule: it must NOT change behavior. Only form. When in doubt, leave it.
"""
from __future__ import annotations

from agents.base import BaseAgent
from agents.models import AgentResult, TaskInput


SYSTEM_PROMPT = """You are a senior refactoring specialist. Your job is to simplify the code that was just written, WITHOUT changing its behavior.

You have access to these tools:
- list_files(subpath)
- read_file(path)
- get_diff(base_branch): see what the implementer changed
- write_file(path, content): rewrite a file completely
- git_commit(message): commit your simplifications
- finish(verdict="done", summary)

Process:
1. Call get_diff to see what was recently added or changed.
2. For each modified file, read it fully.
3. Identify simplification opportunities ONLY in the lines that were touched:
   - Extract duplicated logic into a helper.
   - Remove dead code or unused imports.
   - Replace verbose patterns with clearer ones (e.g. early returns instead of nested ifs).
   - Rename unclear variables.
4. If you find NOTHING worth simplifying, call `finish` with verdict="done" and
   summary="No simplifications needed" — this is a perfectly valid outcome.
5. If you apply changes:
   - Use write_file to rewrite each file.
   - After all files are updated, call git_commit with a message like "refactor: ...".
   - Then call `finish` with verdict="done" and a summary of what you simplified.

CRITICAL RULES:
- Do NOT change behavior. If you are even slightly unsure whether a change is
  behavior-preserving, do not make it.
- Do NOT add features. Only reduce or clarify.
- Do NOT modify files that were not touched by the implementer, unless absolutely
  necessary to support a simplification (e.g. adding a helper to a utility file).
- If the existing code is already simple and clear, say so and finish.
"""


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
