"""
LLM providers saved by the user. JSON persistence with the api_key encrypted
at rest (AES-GCM, master key in env).

Only the orchestrator touches this file. The worker never sees it — when the
user launches a task with `provider_id`, the orchestrator decrypts the key
and puts it into the same one-shot secret store that already exists for the
one-shot flow; from there the pipeline does not change.
"""
from __future__ import annotations

import json
import os
import tempfile
import threading
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from orchestrator.crypto import SealedSecret, open_ as crypto_open, seal as crypto_seal


_DATA_DIR = Path(os.environ.get("DATA_DIR", "/app/data"))
_PROVIDERS_FILE = _DATA_DIR / "providers.json"


@dataclass
class Provider:
    """Secret-free view, safe to return via the API."""
    id: str
    label: str
    provider_label: str
    base_url: str
    model: str
    created_at: str
    # Optional per-role overrides. "" = uses `model`.
    model_implementer: str = ""
    model_reviewer: str = ""
    model_simplifier: str = ""

    def model_for_role(self, role: str) -> str:
        """Returns the role override if it exists; otherwise, the default model."""
        override = getattr(self, f"model_{role}", "") or ""
        return override or self.model


@dataclass
class _StoredProvider:
    id: str
    label: str
    provider_label: str
    base_url: str
    model: str
    created_at: str
    sealed_key: dict = field(default_factory=dict)  # {"nonce": ..., "ciphertext": ...}
    # Per-role overrides. All with default "" for compatibility with entries
    # written before these fields existed.
    model_implementer: str = ""
    model_reviewer: str = ""
    model_simplifier: str = ""

    def public(self) -> Provider:
        return Provider(
            id=self.id,
            label=self.label,
            provider_label=self.provider_label,
            base_url=self.base_url,
            model=self.model,
            created_at=self.created_at,
            model_implementer=self.model_implementer,
            model_reviewer=self.model_reviewer,
            model_simplifier=self.model_simplifier,
        )


class ProvidersStore:
    def __init__(self, path: Path = _PROVIDERS_FILE):
        self._path = path
        self._lock = threading.Lock()
        self._path.parent.mkdir(parents=True, exist_ok=True)

    def _load_all(self) -> list[_StoredProvider]:
        if not self._path.exists():
            return []
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return []
        out: list[_StoredProvider] = []
        for item in raw.get("providers", []):
            try:
                out.append(_StoredProvider(**item))
            except TypeError:
                # Old/corrupted format: skip silently.
                continue
        return out

    def _save_all(self, providers: list[_StoredProvider]) -> None:
        # Atomic write: tmpfile + replace. Avoids leaving the JSON half-written
        # if the process dies mid-write.
        payload = {"providers": [asdict(p) for p in providers]}
        tmp_fd, tmp_path = tempfile.mkstemp(
            prefix=".providers.", suffix=".tmp", dir=str(self._path.parent)
        )
        try:
            with os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2)
            os.chmod(tmp_path, 0o600)
            os.replace(tmp_path, self._path)
        except Exception:
            try:
                os.unlink(tmp_path)
            except FileNotFoundError:
                pass
            raise

    def list(self) -> list[Provider]:
        with self._lock:
            return [p.public() for p in self._load_all()]

    def create(
        self,
        label: str,
        provider_label: str,
        base_url: str,
        model: str,
        api_key: str,
        model_implementer: str = "",
        model_reviewer: str = "",
        model_simplifier: str = "",
    ) -> Provider:
        # Empty `api_key` is allowed (e.g. local Ollama, vLLM without auth).
        # We store an empty sealed_key dict in that case so `get_decrypted_key`
        # can short-circuit without needing to call AES-GCM open on nothing.
        sealed_dict = crypto_seal(api_key).to_dict() if api_key else {}
        entry = _StoredProvider(
            id=str(uuid.uuid4()),
            label=label.strip(),
            provider_label=provider_label.strip() or "custom",
            base_url=base_url.strip(),
            model=model.strip(),
            created_at=datetime.now(timezone.utc).isoformat(),
            sealed_key=sealed_dict,
            model_implementer=model_implementer.strip(),
            model_reviewer=model_reviewer.strip(),
            model_simplifier=model_simplifier.strip(),
        )
        with self._lock:
            all_ = self._load_all()
            all_.append(entry)
            self._save_all(all_)
        return entry.public()

    def update(
        self,
        provider_id: str,
        label: str,
        provider_label: str,
        base_url: str,
        model: str,
        api_key: str | None,
        model_implementer: str = "",
        model_reviewer: str = "",
        model_simplifier: str = "",
    ) -> Provider | None:
        """
        Updates a provider. If `api_key` is None or empty string, keeps the
        existing encrypted key (useful when the user edits without re-entering).
        """
        with self._lock:
            all_ = self._load_all()
            for i, p in enumerate(all_):
                if p.id != provider_id:
                    continue
                new_sealed = p.sealed_key
                if api_key:
                    new_sealed = crypto_seal(api_key).to_dict()
                updated = _StoredProvider(
                    id=p.id,
                    label=label.strip(),
                    provider_label=provider_label.strip() or "custom",
                    base_url=base_url.strip(),
                    model=model.strip(),
                    created_at=p.created_at,
                    sealed_key=new_sealed,
                    model_implementer=model_implementer.strip(),
                    model_reviewer=model_reviewer.strip(),
                    model_simplifier=model_simplifier.strip(),
                )
                all_[i] = updated
                self._save_all(all_)
                return updated.public()
        return None

    def get(self, provider_id: str) -> Provider | None:
        """Public view (without key). Useful to pre-fill the edit form."""
        with self._lock:
            for p in self._load_all():
                if p.id == provider_id:
                    return p.public()
        return None

    def delete(self, provider_id: str) -> bool:
        with self._lock:
            all_ = self._load_all()
            filtered = [p for p in all_ if p.id != provider_id]
            if len(filtered) == len(all_):
                return False
            self._save_all(filtered)
            return True

    def get_decrypted_key(self, provider_id: str) -> tuple[Provider, str] | None:
        """
        Returns (public view, cleartext api_key) or None if it does not exist.
        Empty `sealed_key` (provider stored without a key — e.g. local Ollama)
        yields `""` as the cleartext api_key. Callers that need the key must
        check for emptiness before sending it upstream.

        This call is the only one that extracts the key from the module; the
        result must not be logged or persisted anywhere other than the one-shot
        secret store.
        """
        with self._lock:
            for p in self._load_all():
                if p.id == provider_id:
                    if not p.sealed_key:
                        return p.public(), ""
                    sealed = SealedSecret.from_dict(p.sealed_key)
                    return p.public(), crypto_open(sealed)
        return None


store = ProvidersStore()

