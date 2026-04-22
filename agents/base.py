"""
BaseAgent: loop ReAct genérico para los agentes que NO delegan en Aider.

El implementer no usa esta clase (usa Aider como subprocess).
El reviewer y el simplifier sí.

Flujo:
  1. Mensaje de sistema con el rol + tools disponibles.
  2. Mensaje de usuario con la tarea.
  3. El modelo responde: o tool_calls, o un `finish`.
  4. Si hay tool_calls, los ejecuta y vuelve a llamar al modelo.
  5. Si llama a `finish`, terminamos.
  6. Límite de iteraciones para evitar loops infinitos.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from agents.llm import OllamaClient
from agents.models import AgentResult, AgentRole, LogEntry, TaskInput
from agents.tools.code_tools import TOOL_DEFINITIONS, dispatch


class BaseAgent:
    MAX_ITERATIONS = 20

    def __init__(self, task: TaskInput, system_prompt: str):
        self.task = task
        self.system_prompt = system_prompt
        self.worktree = Path(task.worktree_path)
        self.llm = OllamaClient(host=task.ollama_host, model=task.model)
        self.log: list[LogEntry] = []
        self.commits: list[str] = []

    def build_user_prompt(self) -> str:
        """Override para construir el prompt inicial según el rol."""
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

            # Si no hay tool calls, el modelo ha "contestado" sin decidir.
            # Forzamos que termine con finish o insistimos.
            if not response.tool_calls:
                messages.append({"role": "assistant", "content": response.content})
                messages.append({
                    "role": "user",
                    "content": "You must either call a tool or call `finish` to end the task.",
                })
                continue

            # Procesar cada tool call
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

                # `finish` es terminal
                if name == "finish":
                    return self._build_result(args)

                # Resto de tools
                result = dispatch(name, args, self.worktree)

                # Capturar hashes de commit si los hay
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

        # Se acabaron las iteraciones sin `finish`
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
