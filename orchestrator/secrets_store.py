"""
In-memory one-shot secret handoff.

Why: the API key that the user enters in the front-end cannot enter Prefect
(it would remain in the flow run parameters, visible in the UI) nor be
written to `.task-input.json` (it stays in the worktree on disk) nor be
logged. This module stores the key only in the orchestrator's RAM, returns
an opaque token, and allows it to be consumed ONCE against an authenticated
internal endpoint.

Flow:
  1. POST /tasks receives api_key -> stash() -> secret_token.
  2. The orchestrator passes the token (not the key) as a flow parameter.
  3. The worker, on starting the flow, calls POST /internal/consume-secret
     with the token + internal auth header -> consume() -> receives the key once.
  4. The worker injects it as OPENAI_API_KEY in the agent container env and
     keeps it in memory of the flow process until it ends.

If the orchestrator restarts, in-flight flows fail on consume (nonexistent
token). That is better than persisting keys to disk.
"""
from __future__ import annotations

import secrets
import threading
import time
from dataclasses import dataclass


_TTL_SECONDS = 600  # 10 min: grace period between stash (POST /tasks) and
                    # consume (start of the flow in the worker). With pool
                    # backlog, a flow can wait several minutes before starting.


@dataclass
class _Entry:
    api_key: str
    expires_at: float


class SecretStore:
    def __init__(self, ttl_seconds: int = _TTL_SECONDS):
        self._entries: dict[str, _Entry] = {}
        self._lock = threading.Lock()
        self._ttl = ttl_seconds

    def stash(self, api_key: str) -> str:
        token = secrets.token_urlsafe(32)
        with self._lock:
            self._entries[token] = _Entry(
                api_key=api_key,
                expires_at=time.monotonic() + self._ttl,
            )
        return token

    def consume(self, token: str) -> str | None:
        """One-shot: returns the key and deletes the entry. None if it does not exist or has expired."""
        now = time.monotonic()
        with self._lock:
            entry = self._entries.pop(token, None)
        if entry is None:
            return None
        if entry.expires_at < now:
            return None
        return entry.api_key

    def discard(self, token: str) -> None:
        """Delete without consuming (e.g. if the flow fails before starting)."""
        with self._lock:
            self._entries.pop(token, None)

    def reap(self) -> int:
        """Deletes expired entries. Returns how many it has cleaned."""
        now = time.monotonic()
        with self._lock:
            expired = [t for t, e in self._entries.items() if e.expires_at < now]
            for t in expired:
                del self._entries[t]
        return len(expired)

    def size(self) -> int:
        with self._lock:
            return len(self._entries)


# Global instance of the orchestrator process.
store = SecretStore()
