from __future__ import annotations

import pytest

from app.stl_drafts import (
    StlDraftAction,
    StlDraftCache,
    StlDraftExpired,
    StlDraftUnknown,
    validate_stl_action,
)


def test_put_creates_source_and_current_paths(tmp_path):
    cache = StlDraftCache(root=tmp_path, ttl_seconds=3600)

    draft = cache.put_source(b"solid test\nendsolid test\n", "part.stl")

    assert draft.token
    assert draft.filename == "part.stl"
    assert draft.source_path.exists()
    assert draft.source_path.read_bytes().startswith(b"solid")
    assert draft.current_3mf_path.name == "current.3mf"
    assert draft.next_3mf_path().name == "next.3mf"


def test_get_unknown_raises(tmp_path):
    cache = StlDraftCache(root=tmp_path, ttl_seconds=3600)

    with pytest.raises(StlDraftUnknown):
        cache.get("missing")


def test_expired_draft_is_deleted(tmp_path, monkeypatch):
    cache = StlDraftCache(root=tmp_path, ttl_seconds=10)
    draft = cache.put_source(b"solid test\nendsolid test\n", "part.stl")
    monkeypatch.setattr("app.stl_drafts.time.time", lambda: draft.created_at + 11)

    with pytest.raises(StlDraftExpired):
        cache.get(draft.token)
    assert not draft.root.exists()


def test_validate_stl_action_accepts_known_actions():
    assert validate_stl_action("auto_orient") == StlDraftAction.AUTO_ORIENT
    assert validate_stl_action("rotate_z_90") == StlDraftAction.ROTATE_Z_90
    assert validate_stl_action("rotate_z_minus_90") == StlDraftAction.ROTATE_Z_MINUS_90
    assert validate_stl_action("center") == StlDraftAction.CENTER
    assert validate_stl_action("arrange") == StlDraftAction.ARRANGE
    assert validate_stl_action("reset") == StlDraftAction.RESET


def test_validate_stl_action_rejects_unknown_action():
    with pytest.raises(ValueError, match="invalid STL layout action"):
        validate_stl_action("drag")
