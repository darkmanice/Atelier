"""
Secret handoff worker -> orchestrator (inside the flow).

The flow calls `fetch_api_key(secret_token)` once at startup. The resulting
key lives only in the flow stack during execution and is passed as an
argument to each `run_agent` invocation without being persisted to disk.
"""
from __future__ import annotations

import os

import httpx


# Inside the compose network, the orchestrator is reachable by service name.
# The worker and the orchestrator are in the same "pipeline" network.
_ORCHESTRATOR_INTERNAL_URL = os.environ.get(
    "ORCHESTRATOR_INTERNAL_URL", "http://orchestrator:8000"
)
_INTERNAL_TOKEN = os.environ.get("INTERNAL_API_TOKEN", "").strip()


class SecretHandoffError(RuntimeError):
    pass


def fetch_api_key(secret_token: str | None) -> str:
    """
    Consume the one-shot token against the orchestrator and return the key.

    If `secret_token` is None or empty, returns an empty string (endpoint without auth).
    Any other failure raises SecretHandoffError so the flow dies cleanly.
    """
    if not secret_token:
        return ""
    if not _INTERNAL_TOKEN:
        raise SecretHandoffError(
            "INTERNAL_API_TOKEN not set in worker env; cannot consume secret"
        )

    try:
        resp = httpx.post(
            f"{_ORCHESTRATOR_INTERNAL_URL}/internal/consume-secret",
            json={"token": secret_token},
            headers={"X-Internal-Token": _INTERNAL_TOKEN},
            timeout=10.0,
        )
    except httpx.HTTPError as e:
        raise SecretHandoffError(f"Cannot reach orchestrator: {e}") from e

    if resp.status_code != 200:
        # We do not dump the body so we do not log messages that could correlate
        # tokens. Only the status code.
        raise SecretHandoffError(
            f"Secret consume failed with status {resp.status_code}"
        )

    api_key = resp.json().get("api_key", "")
    if not api_key:
        raise SecretHandoffError("Empty api_key returned by orchestrator")
    return api_key
