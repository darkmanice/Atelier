"""
Minimal LLM client, generic against OpenAI-compatible endpoints.

- Uses the official `openai` SDK as transport (supports Chat Completions +
  standard tool calling). Works against OpenAI, NVIDIA (build.nvidia.com),
  OpenRouter, Groq, DeepSeek, vLLM, LM Studio and any compatible gateway.
  Ollama too, via its `/v1` endpoint.
- Reads `base_url` and `api_key` from the container environment
  (OPENAI_API_BASE / OPENAI_API_KEY). The orchestrator injects them when
  launching the agent; this way the key never touches `.task-input.json` or
  the worktree.
- Returns its own `LLMResponse` type, not the raw SDK response, so the rest
  of the code does not depend on the specific library.
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass
from typing import Any

from openai import APIConnectionError, APIError, OpenAI, RateLimitError


@dataclass
class LLMResponse:
    content: str
    tool_calls: list[dict[str, Any]]   # [{"id": ..., "name": ..., "arguments": {...}}, ...]
    raw: dict                           # raw response in case debugging is needed


class LLMClient:
    def __init__(self, model: str, base_url: str | None = None, api_key: str | None = None):
        # Priority: explicit args > env. The normal path is "via env var
        # injected by the worker". The args exist for tests/debug.
        resolved_base = base_url or os.environ.get("OPENAI_API_BASE") or ""
        resolved_key = api_key or os.environ.get("OPENAI_API_KEY") or "sk-no-auth"
        if not resolved_base:
            raise RuntimeError(
                "LLMClient: missing base_url (set OPENAI_API_BASE or pass base_url=)"
            )
        self.client = OpenAI(base_url=resolved_base, api_key=resolved_key)
        self.model = model

    def chat(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        temperature: float = 0.2,
        max_retries: int = 2,
    ) -> LLMResponse:
        """
        Call the model. `tools` are tool definitions in OpenAI format.
        Normalize the output: returns `content` and `tool_calls` flattened to
        `[{"name": ..., "arguments": dict}, ...]` as `OllamaClient` did.
        """
        last_error: Exception | None = None
        for attempt in range(max_retries + 1):
            try:
                resp = self.client.chat.completions.create(
                    model=self.model,
                    messages=messages,
                    tools=_wrap_tools(tools) if tools else None,
                    temperature=temperature,
                )
                break
            except (APIConnectionError, RateLimitError, APIError) as e:
                last_error = e
                if attempt == max_retries:
                    raise
                time.sleep(1.5 * (attempt + 1))
        else:
            assert last_error is not None
            raise last_error

        choice = resp.choices[0]
        msg = choice.message

        tool_calls: list[dict[str, Any]] = []
        for tc in (msg.tool_calls or []):
            # arguments comes as a JSON-string per the OpenAI contract.
            import json
            try:
                args = json.loads(tc.function.arguments or "{}")
            except json.JSONDecodeError:
                args = {"_raw": tc.function.arguments}
            # Preserve the id so we can echo it back in the assistant turn
            # and reference it from the tool-response messages (required by
            # the strict OpenAI Chat Completions schema).
            tool_calls.append({
                "id": tc.id,
                "name": tc.function.name,
                "arguments": args,
            })

        return LLMResponse(
            content=msg.content or "",
            tool_calls=tool_calls,
            raw=resp.model_dump(),
        )


def _wrap_tools(tools: list[dict]) -> list[dict]:
    """
    Accepts both the Ollama "raw" format (each tool is already `{type, function}`)
    and the "function-only" format (each tool is `{name, description, parameters}`).
    The OpenAI SDK requires `{type: "function", function: {...}}`.
    """
    wrapped: list[dict] = []
    for t in tools:
        if "function" in t and "type" in t:
            wrapped.append(t)
        else:
            wrapped.append({"type": "function", "function": t})
    return wrapped
