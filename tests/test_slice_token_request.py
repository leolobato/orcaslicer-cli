"""Validation tests for the SliceTokenRequest pydantic model.

The `copies` field defaults to 1, accepts integers 1..100, and rejects
anything else. Bounds are defense-in-depth — the C++ binary also clamps
to 1..100 (see cpp/src/json_io.cpp).
"""
import pytest
from pydantic import ValidationError

from app.main import SliceTokenRequest


def _base_kwargs():
    return dict(
        input_token="abc123",
        machine_id="MACH",
        process_id="PROC",
        filament_settings_ids=["GFL00"],
    )


def test_copies_defaults_to_1():
    req = SliceTokenRequest(**_base_kwargs())
    assert req.copies == 1


def test_copies_accepts_integers_in_range():
    for n in (1, 2, 50, 100):
        req = SliceTokenRequest(copies=n, **_base_kwargs())
        assert req.copies == n


def test_copies_rejects_zero():
    with pytest.raises(ValidationError):
        SliceTokenRequest(copies=0, **_base_kwargs())


def test_copies_rejects_negative():
    with pytest.raises(ValidationError):
        SliceTokenRequest(copies=-1, **_base_kwargs())


def test_copies_rejects_over_max():
    with pytest.raises(ValidationError):
        SliceTokenRequest(copies=101, **_base_kwargs())


def test_copies_rejects_non_integer():
    with pytest.raises(ValidationError):
        SliceTokenRequest(copies="four", **_base_kwargs())
