"""Content-addressed token cache for 3MF files.

Files are stored on disk by SHA-256. Tokens are opaque IDs that map to a SHA.
Eviction is LRU, gated on configurable byte and file-count caps.
"""

from __future__ import annotations

import hashlib
import os
import secrets
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path


@dataclass
class _Entry:
    token: str
    sha: str
    size: int
    last_access: float


class TokenCache:
    def __init__(self, cache_dir: Path, max_bytes: int, max_files: int) -> None:
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.max_bytes = max_bytes
        self.max_files = max_files
        self._lock = threading.RLock()
        self._entries: OrderedDict[str, _Entry] = OrderedDict()
        self._sha_to_token: dict[str, str] = {}

    def put(self, payload: bytes) -> tuple[str, str, int, list[str]]:
        sha = hashlib.sha256(payload).hexdigest()
        size = len(payload)
        with self._lock:
            if sha in self._sha_to_token:
                token = self._sha_to_token[sha]
                entry = self._entries[token]
                entry.last_access = time.time()
                self._entries.move_to_end(token)
                return token, sha, size, []
            token = secrets.token_urlsafe(16)
            path = self._path_for_sha(sha)
            if not path.exists():
                tmp = path.parent / (path.name + f".tmp-{os.getpid()}-{secrets.token_hex(4)}")
                tmp.write_bytes(payload)
                os.replace(tmp, path)
            self._entries[token] = _Entry(token, sha, size, time.time())
            self._sha_to_token[sha] = token
            evicted = self._evict_if_needed()
            return token, sha, size, evicted

    def _evict_if_needed(self) -> list[str]:
        evicted: list[str] = []
        # File count cap — treat max_files=0 as floor of 1 so the newly-inserted
        # entry is never self-evicted.
        while len(self._entries) > max(self.max_files, 1) and len(self._entries) > 1:
            tok, entry = self._entries.popitem(last=False)
            self._sha_to_token.pop(entry.sha, None)
            self._path_for_sha(entry.sha).unlink(missing_ok=True)
            evicted.append(tok)
        # Byte cap — stop at 1 entry so an oversized payload doesn't self-evict.
        total = sum(e.size for e in self._entries.values())
        while total > self.max_bytes and len(self._entries) > 1:
            tok, entry = self._entries.popitem(last=False)
            self._sha_to_token.pop(entry.sha, None)
            self._path_for_sha(entry.sha).unlink(missing_ok=True)
            total -= entry.size
            evicted.append(tok)
        return evicted

    def path(self, token: str) -> Path:
        with self._lock:
            entry = self._entries.get(token)
            if entry is None:
                raise KeyError(token)
            self._entries.move_to_end(token)
            entry.last_access = time.time()
            return self._path_for_sha(entry.sha)

    def _path_for_sha(self, sha: str) -> Path:
        return self.cache_dir / f"{sha}.3mf"
