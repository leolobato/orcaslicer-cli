"""On-disk cache for preview-first STL draft sessions."""

from __future__ import annotations

import re
import secrets
import shutil
import time
import json
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path


class StlDraftUnknown(KeyError):
    """Raised when a draft token is not known to this process."""


class StlDraftExpired(KeyError):
    """Raised when a draft token existed but exceeded its TTL."""


class StlDraftAction(StrEnum):
    AUTO_ORIENT = "auto_orient"
    ROTATE_X_90 = "rotate_x_90"
    ROTATE_X_MINUS_90 = "rotate_x_minus_90"
    ROTATE_Y_90 = "rotate_y_90"
    ROTATE_Y_MINUS_90 = "rotate_y_minus_90"
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
    _METADATA_FILE = "draft.json"

    def __init__(self, root: Path, ttl_seconds: int) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.ttl_seconds = ttl_seconds
        self._drafts: dict[str, StlDraft] = {}
        self.sweep_expired()

    def put_source(self, payload: bytes, filename: str | None) -> StlDraft:
        self.sweep_expired()
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
        try:
            draft.source_path.write_bytes(payload)
            self._write_metadata(draft)
        except Exception:
            shutil.rmtree(root)
            raise
        self._drafts[token] = draft
        return draft

    def get(self, token: str) -> StlDraft:
        draft = self._drafts.get(token)
        if draft is None:
            raise StlDraftUnknown(token)
        now = time.time()
        if now - draft.created_at > self.ttl_seconds:
            try:
                self.delete(token)
            except Exception as exc:
                raise StlDraftExpired(token) from exc
            raise StlDraftExpired(token)
        # last_access is observability/LRU metadata; TTL is fixed from created_at.
        refreshed = StlDraft(
            token=draft.token,
            root=draft.root,
            filename=draft.filename,
            created_at=draft.created_at,
            last_access=now,
        )
        self._drafts[token] = refreshed
        self._write_metadata(refreshed)
        return refreshed

    def delete(self, token: str) -> bool:
        draft = self._drafts.get(token)
        if draft is None:
            return False
        shutil.rmtree(draft.root)
        self._drafts.pop(token)
        return True

    def sweep_expired(self) -> None:
        now = time.time()
        for token, draft in list(self._drafts.items()):
            if now - draft.created_at <= self.ttl_seconds:
                continue
            self.delete(token)

        for child in self.root.iterdir():
            if not child.is_dir() or child.name in self._drafts:
                continue
            created_at = self._created_at_from_disk(child)
            if now - created_at > self.ttl_seconds:
                shutil.rmtree(child)

    def _write_metadata(self, draft: StlDraft) -> None:
        (draft.root / self._METADATA_FILE).write_text(
            json.dumps({
                "token": draft.token,
                "filename": draft.filename,
                "created_at": draft.created_at,
                "last_access": draft.last_access,
            })
        )

    def _created_at_from_disk(self, root: Path) -> float:
        metadata = root / self._METADATA_FILE
        try:
            raw = json.loads(metadata.read_text())
            created_at = raw.get("created_at")
            if isinstance(created_at, (int, float)):
                return float(created_at)
        except Exception:
            pass
        return root.stat().st_mtime
