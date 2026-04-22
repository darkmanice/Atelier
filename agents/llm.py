"""
Cliente Ollama mínimo y compartido.

- Abstrae la llamada a /api/chat con tool-calling.
- Hace retry ligero en errores transitorios de red.
- Devuelve un tipo estructurado en vez del dict crudo de la librería.

Nota: usamos la librería oficial `ollama` como transporte, pero no exponemos
su tipo de respuesta fuera de este módulo. Así, si mañana cambiamos a Claude
API o a vLLM directamente, solo tocamos este fichero.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from ollama import Client


@dataclass
class LLMResponse:
    content: str
    tool_calls: list[dict[str, Any]]   # [{"name": ..., "arguments": {...}}, ...]
    raw: dict                           # respuesta bruta por si hace falta debug


class OllamaClient:
    def __init__(self, host: str, model: str, num_ctx: int = 32768):
        self.client = Client(host=host)
        self.model = model
        self.num_ctx = num_ctx

    def chat(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        temperature: float = 0.2,
        max_retries: int = 2,
    ) -> LLMResponse:
        """
        Llama al modelo. `tools` es la lista de tool definitions en formato OpenAI/Ollama.
        Devuelve tanto el content como los tool_calls parseados.
        """
        options = {
            "temperature": temperature,
            "num_ctx": self.num_ctx,
        }

        last_error: Exception | None = None
        for attempt in range(max_retries + 1):
            try:
                resp = self.client.chat(
                    model=self.model,
                    messages=messages,
                    tools=tools,
                    options=options,
                )
                break
            except Exception as e:
                last_error = e
                if attempt == max_retries:
                    raise
                time.sleep(1.5 * (attempt + 1))
        else:
            assert last_error is not None
            raise last_error

        msg = resp.get("message", {})
        return LLMResponse(
            content=msg.get("content", "") or "",
            tool_calls=[
                {"name": tc["function"]["name"], "arguments": tc["function"]["arguments"]}
                for tc in (msg.get("tool_calls") or [])
            ],
            raw=dict(resp),
        )
