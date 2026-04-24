"""
BaseAgent: generic ReAct loop for agents that do NOT delegate to Aider.

The implementer does not use this class (it uses Aider as a subprocess).
The reviewer and simplifier do.

Flow:
  1. System message with the role + available tools.
  2. User message with the task.
  3. The model responds: either tool_calls, or a `finish`.
  4. If there are tool_calls, execute them and call the model again.
  5. If it calls `finish`, we terminate.
  6. Iteration limit to avoid infinite loops.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from agents.llm import LLMClient
from agents.models import AgentResult, AgentRole, LogEntry, TaskInput
from agents.tools.code_tools import TOOL_DEFINITIONS, dispatch


class BaseAgent:
    MAX_ITERATIONS = 20

    def __init__(self, task: TaskInput, system_prompt: str):
        self.task = task
        self.system_prompt = system_prompt
        self.worktree = Path(task.worktree_path)
        # base_url and api_key come from the environment (injected by the worker).
        self.llm = LLMClient(model=task.model)
        self.log: list[LogEntry] = []
        self.commits: list[str] = []

    def build_user_prompt(self) -> str:
        """Override to build the initial prompt according to the role."""
        raise NotImplementedError

    def run(self) -> AgentResult:
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": self.system_prompt},
            {"role": "user", "content": self.build_user_prompt()},
        ]
        self.log.append(LogEntry(role=self.task.role, kind="info", content="Starting loop"))

        for iteration in range(self.MAX_ITERATIONS):
            response = self.llm.chat(messages=messages, tools=TOOL_DEFINITIONS)

            if response.content:
                self.log.append(LogEntry(
                    role=self.task.role,
                    kind="llm_message",
                    content=response.content,
                    metadata={"iteration": iteration},
                ))

            # If there are no tool calls, the model has "answered" without deciding.
            # Force it to end with finish or insist.
            if not response.tool_calls:
                messages.append({"role": "assistant", "content": response.content})
                messages.append({
                    "role": "user",
                    "content": "You must either call a tool or call `finish` to end the task.",
                })
                continue

            # Process each tool call
            messages.append({
                "role": "assistant",
                "content": response.content,
                "tool_calls": [
                    {"function": {"name": tc["name"], "arguments": tc["arguments"]}}
                    for tc in response.tool_calls
                ],
            })

            for tc in response.tool_calls:
                name = tc["name"]
                args = tc["arguments"] if isinstance(tc["arguments"], dict) else json.loads(tc["arguments"])

                self.log.append(LogEntry(
                    role=self.task.role,
                    kind="tool_call",
                    content=f"{name}({json.dumps(args, ensure_ascii=False)[:500]})",
                ))

                # `finish` is terminal
                if name == "finish":
                    return self._build_result(args)

                # Remaining tools
                result = dispatch(name, args, self.worktree)

                # Capture commit hashes if any
                if name == "git_commit" and result.startswith("committed "):
                    sha = result.split()[1].rstrip(":")
                    self.commits.append(sha)

                self.log.append(LogEntry(
                    role=self.task.role,
                    kind="tool_result",
                    content=result[:2000],
                ))

                messages.append({
                    "role": "tool",
                    "content": result,
                    "name": name,
                })

        # Iterations exhausted without `finish`
        return AgentResult(
            success=False,
            verdict="failed",
            summary=f"Exceeded {self.MAX_ITERATIONS} iterations without finish",
            log=self.log,
            commits=self.commits,
        )

    def _build_result(self, finish_args: dict) -> AgentResult:
        verdict = finish_args.get("verdict", "done")
        summary = finish_args.get("summary", "")
        comments = finish_args.get("comments")

        return AgentResult(
            success=verdict in ("approved", "done"),
            verdict=verdict,
            summary=summary,
            log=self.log,
            commits=self.commits,
            review_comments=comments,
        )
