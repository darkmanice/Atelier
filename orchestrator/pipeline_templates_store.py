"""
Pipeline canvas templates saved by the user. Plain JSON on disk — no secrets,
so no encryption. Same atomic-write + lock pattern as `providers_store`.

Each template is `{id, name, spec, created_at}`. The `spec` mirrors the runtime
contract (see `_parse_pipeline_spec` in `orchestrator/main.py`): an ordered
list of `{type, id}` dicts. Kept opaque here so the store stays dumb; the API
layer is the one that validates types against the block catalog.
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


_DATA_DIR = Path(os.environ.get("DATA_DIR", "/app/data"))
_TEMPLATES_FILE = _DATA_DIR / "pipeline_templates.json"


@dataclass
class PipelineTemplate:
    id: str
    name: str
    spec: list[dict]
    created_at: str
    # Kept for forward-compat: lets the UI show "X steps" without parsing spec.
    step_count: int = 0


@dataclass
class _StoredTemplate:
    id: str
    name: str
    spec: list[dict] = field(default_factory=list)
    created_at: str = ""

    def public(self) -> PipelineTemplate:
        return PipelineTemplate(
            id=self.id,
            name=self.name,
            spec=list(self.spec),
            created_at=self.created_at,
            step_count=len(self.spec),
        )


class PipelineTemplatesStore:
    def __init__(self, path: Path = _TEMPLATES_FILE):
        self._path = path
        self._lock = threading.Lock()
        self._path.parent.mkdir(parents=True, exist_ok=True)

    def _load_all(self) -> list[_StoredTemplate]:
        if not self._path.exists():
            return []
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return []
        out: list[_StoredTemplate] = []
        for item in raw.get("templates", []):
            try:
                out.append(_StoredTemplate(**item))
            except TypeError:
                continue
        return out

    def _save_all(self, templates: list[_StoredTemplate]) -> None:
        payload = {"templates": [asdict(t) for t in templates]}
        tmp_fd, tmp_path = tempfile.mkstemp(
            prefix=".pipeline_templates.", suffix=".tmp", dir=str(self._path.parent)
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

    def list(self) -> list[PipelineTemplate]:
        with self._lock:
            return [t.public() for t in self._load_all()]

    def get(self, template_id: str) -> PipelineTemplate | None:
        with self._lock:
            for t in self._load_all():
                if t.id == template_id:
                    return t.public()
        return None

    def create(self, name: str, spec: list[dict]) -> PipelineTemplate:
        entry = _StoredTemplate(
            id=str(uuid.uuid4()),
            name=name.strip(),
            spec=list(spec),
            created_at=datetime.now(timezone.utc).isoformat(),
        )
        with self._lock:
            all_ = self._load_all()
            all_.append(entry)
            self._save_all(all_)
        return entry.public()

    def update(self, template_id: str, name: str, spec: list[dict]) -> PipelineTemplate | None:
        with self._lock:
            all_ = self._load_all()
            for i, t in enumerate(all_):
                if t.id != template_id:
                    continue
                updated = _StoredTemplate(
                    id=t.id,
                    name=name.strip(),
                    spec=list(spec),
                    created_at=t.created_at,
                )
                all_[i] = updated
                self._save_all(all_)
                return updated.public()
        return None

    def delete(self, template_id: str) -> bool:
        with self._lock:
            all_ = self._load_all()
            filtered = [t for t in all_ if t.id != template_id]
            if len(filtered) == len(all_):
                return False
            self._save_all(filtered)
            return True


store = PipelineTemplatesStore()
