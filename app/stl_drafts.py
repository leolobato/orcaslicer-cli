"""On-disk cache for preview-first STL draft sessions."""

from __future__ import annotations

import re
import secrets
import shutil
import time
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path


class StlDraftUnknown(KeyError):
    """Raised when a draft token is not known to this process."""


class StlDraftExpired(KeyError):
    """Raised when a draft token existed but exceeded its TTL."""


class StlDraftAction(StrEnum):
    AUTO_ORIENT = "auto_orient"
    ROTATE_Z_90 = "rotate_z_90"
    ROTATE_Z_MINUS_90 = "rotate_z_minus_90"
    CENTER = "center"
    ARRANGE = "arrange"
    RESET = "reset"


_SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9._-]+")


def _safe_filename(name: str | None) -> str:
    raw = (name or "model.stl").split("/")[-1].split("\\")[-1].strip()
    safe = _SAFE_NAME_RE.sub("_", raw) or "model.stl"
    return safe if safe.lower().endswith(".stl") else f"{safe}.stl"


def validate_stl_action(value: str) -> StlDraftAction:
    try:
        return StlDraftAction(value)
    except ValueError as exc:
        allowed = ", ".join(a.value for a in StlDraftAction)
        raise ValueError(f"invalid STL layout action {value!r}; allowed: {allowed}") from exc


@dataclass(frozen=True)
class StlDraft:
    token: str
    root: Path
    filename: str
    created_at: float
    last_access: float

    @property
    def source_path(self) -> Path:
        return self.root / "source.stl"

    @property
    def current_3mf_path(self) -> Path:
        return self.root / "current.3mf"

    @property
    def scene_path(self) -> Path:
        return self.root / "scene.json"

    def next_3mf_path(self) -> Path:
        return self.root / "next.3mf"


class StlDraftCache:
    def __init__(self, root: Path, ttl_seconds: int) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.ttl_seconds = ttl_seconds
        self._drafts: dict[str, StlDraft] = {}

    def put_source(self, payload: bytes, filename: str | None) -> StlDraft:
        token = secrets.token_urlsafe(16)
        now = time.time()
        root = self.root / token
        root.mkdir(parents=True, exist_ok=False)
        draft = StlDraft(
            token=token,
            root=root,
            filename=_safe_filename(filename),
            created_at=now,
            last_access=now,
        )
        draft.source_path.write_bytes(payload)
        self._drafts[token] = draft
        return draft

    def get(self, token: str) -> StlDraft:
        draft = self._drafts.get(token)
        if draft is None:
            raise StlDraftUnknown(token)
        now = time.time()
        if now - draft.created_at > self.ttl_seconds:
            self.delete(token)
            raise StlDraftExpired(token)
        refreshed = StlDraft(
            token=draft.token,
            root=draft.root,
            filename=draft.filename,
            created_at=draft.created_at,
            last_access=now,
        )
        self._drafts[token] = refreshed
        return refreshed

    def delete(self, token: str) -> bool:
        draft = self._drafts.pop(token, None)
        if draft is None:
            return False
        shutil.rmtree(draft.root, ignore_errors=True)
        return True
