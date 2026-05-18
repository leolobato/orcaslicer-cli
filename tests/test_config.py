from app import config as cfg


def test_default_stl_draft_ttl_is_24_hours():
    assert cfg.STL_DRAFT_TTL_SECONDS == 24 * 60 * 60
