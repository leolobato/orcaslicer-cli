"""Process-domain option metadata and layout for the parameter editor.

Loads two pieces of data at startup (and on /profiles/reload):

  1. The full per-option metadata catalogue, by shelling out to
     ``orca-headless dump-options``. Served verbatim at
     ``GET /options/process``.

  2. The page → optgroup → option layout extracted at build time from
     ``Tab.cpp::TabPrint::build()`` (lives at
     ``cpp/src/generated/process_pages.json``). When
     ``cfg.PROCESS_ALLOWLIST_ENABLED`` is true the layout is filtered by
     ``app/process_allowlist.json`` (only allowlisted keys survive, empty
     optgroups and pages are dropped); otherwise the full GUI layout is
     served verbatim. Reachable at ``GET /options/process/layout``.

Both pieces are cached in module-level state. The metadata catalogue is
unfiltered so iOS/web can render labels for any modified key (the editor
shows non-allowlisted ones read-only when filtering is on).
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import config as cfg
from .binary_client import BinaryClient

logger = logging.getLogger(__name__)

_PKG_ROOT = Path(__file__).resolve().parent
_REPO_ROOT = _PKG_ROOT.parent

_LAYOUT_PATH = _REPO_ROOT / "cpp" / "src" / "generated" / "process_pages.json"
_ALLOWLIST_PATH = _PKG_ROOT / "process_allowlist.json"


@dataclass
class OptionsCache:
    """Module-level snapshot served by the /options/* endpoints."""
    metadata: dict[str, Any] = field(default_factory=dict)
    layout: dict[str, Any] = field(default_factory=dict)


_cache = OptionsCache()


def filter_layout(
    pages: list[dict[str, Any]], allowed: set[str],
) -> list[dict[str, Any]]:
    """Drop options not in ``allowed``; drop optgroups and pages that empty out.

    Preserves option ordering within each surviving optgroup, and optgroup
    ordering within each surviving page.
    """
    result: list[dict[str, Any]] = []
    for page in pages:
        survived_optgroups: list[dict[str, Any]] = []
        for og in page.get("optgroups", []):
            kept = [k for k in og.get("options", []) if k in allowed]
            if kept:
                survived_optgroups.append({
                    "label": og.get("label", ""),
                    "options": kept,
                })
        if survived_optgroups:
            result.append({
                "label": page.get("label", ""),
                "optgroups": survived_optgroups,
            })
    return result


def _build_metadata(catalogue: dict[str, Any], api_version: str) -> dict[str, Any]:
    """Reshape the dump-options catalogue into the /options/process payload.

    The C++ side emits a list ``options: [{key,...}, ...]`` for streaming
    friendliness; the API serves it as a dict keyed by ``key`` for cheap
    client-side lookup.
    """
    return {
        "version": api_version,
        "options": {opt["key"]: opt for opt in catalogue.get("options", [])},
    }


def _build_layout(
    layout_doc: dict[str, Any],
    allowlist_doc: dict[str, Any] | None,
    api_version: str,
) -> dict[str, Any]:
    pages_in = layout_doc.get("pages", [])
    if allowlist_doc is None:
        # Allowlist disabled — return the GUI layout verbatim.
        return {
            "version": api_version,
            "allowlist_revision": "",
            "pages": pages_in,
        }
    pages = filter_layout(
        pages_in,
        set(allowlist_doc.get("options", [])),
    )
    return {
        "version": api_version,
        "allowlist_revision": allowlist_doc.get("revision", ""),
        "pages": pages,
    }


async def load_options_cache(*, binary_client: BinaryClient) -> OptionsCache:
    """Refresh the module-level cache. Call from app startup and /reload."""
    api_version = f"{cfg.ORCA_VERSION}-{cfg.API_REVISION}"

    catalogue = await binary_client.dump_options()
    metadata = _build_metadata(catalogue, api_version)

    if not _LAYOUT_PATH.exists():
        raise RuntimeError(
            f"process_pages.json missing at {_LAYOUT_PATH}; "
            "run scripts/extract_tab_layout.py")
    layout_doc = json.loads(_LAYOUT_PATH.read_text())

    allowlist_doc: dict[str, Any] | None = None
    if cfg.PROCESS_ALLOWLIST_ENABLED:
        if not _ALLOWLIST_PATH.exists():
            raise RuntimeError(
                f"process_allowlist.json missing at {_ALLOWLIST_PATH}")
        allowlist_doc = json.loads(_ALLOWLIST_PATH.read_text())

    layout = _build_layout(layout_doc, allowlist_doc, api_version)

    metadata_keys = set(metadata["options"].keys())
    if allowlist_doc is not None:
        # Drop a warning for any allowlist key that doesn't appear in the
        # metadata dump — script check_allowlist.py is the strict gate; here
        # we only log so a curated allowlist mistake doesn't crash startup.
        for key in allowlist_doc.get("options", []):
            if key not in metadata_keys:
                logger.warning(
                    "allowlist references key %r which is not in dump-options "
                    "(typo, removed upstream, or non-process-domain key)", key)

    _cache.metadata = metadata
    _cache.layout = layout
    exposed_keys = sum(
        len(og["options"]) for p in layout["pages"] for og in p["optgroups"])
    logger.info(
        "options cache loaded: %d metadata entries, %d layout keys across "
        "%d pages (allowlist %s)",
        len(metadata["options"]),
        exposed_keys,
        len(layout["pages"]),
        "enabled" if allowlist_doc is not None else "disabled",
    )
    return _cache


def get_metadata() -> dict[str, Any]:
    """Return the cached /options/process payload."""
    return _cache.metadata


def get_layout() -> dict[str, Any]:
    """Return the cached /options/process/layout payload."""
    return _cache.layout
