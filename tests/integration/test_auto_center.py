"""Auto-center regression test using the launcher fixture.

Fixture 08 is a 3MF authored for a Bambu P2S (256mm bed) with three
objects sitting at world X = 181/189/231 mm. Sliced as-authored against
A1 mini's 180mm bed, the rightmost object (X=231) extrudes off the bed.

This test slices fixture 08 through `/slice/v2` with
``auto_center=True`` against the A1 mini, then asserts every toolpath
XY in the resulting gcode lies inside A1 mini's 180×180 mm printable
area. Without auto-centering, X coordinates would exceed 180.

The auto_center path delegates to libslic3r's
``Model::center_instances_around_point`` + ``ModelObject::ensure_on_bed``
(see `cpp/src/slice_mode.cpp::auto_center_on_plate`).

Opt-in: requires a running container reachable at ``$ORCASLICER_API``
(default ``http://localhost:8070``). Skipped when not reachable.
"""
from __future__ import annotations

import io
import json
import os
import re
import urllib.request
import uuid
import zipfile
from pathlib import Path

import pytest

API = os.environ.get("ORCASLICER_API", "http://localhost:8070")
FIXTURE_DIR = Path(__file__).resolve().parents[2].parent / "_fixture"

# A1 mini build volume (origin at front-left, +X right, +Y back).
A1M_BED_X = 180.0
A1M_BED_Y = 180.0


def _container_reachable() -> bool:
    try:
        with urllib.request.urlopen(f"{API}/health", timeout=2.0) as r:
            return r.status == 200
    except Exception:
        return False


pytestmark = pytest.mark.skipif(
    not _container_reachable(),
    reason=f"orcaslicer-cli not reachable at {API}",
)


def _post_multipart_file(url: str, file_path: Path) -> dict:
    boundary = f"----pytest{uuid.uuid4().hex}"
    body = io.BytesIO()
    body.write(f"--{boundary}\r\n".encode())
    body.write(
        f'Content-Disposition: form-data; name="file"; '
        f'filename="{file_path.name}"\r\n'.encode()
    )
    body.write(b"Content-Type: application/octet-stream\r\n\r\n")
    body.write(file_path.read_bytes())
    body.write(f"\r\n--{boundary}--\r\n".encode())
    req = urllib.request.Request(
        url,
        data=body.getvalue(),
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=120.0) as r:
        return json.loads(r.read().decode())


def _post_json(url: str, payload: dict) -> dict:
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=180.0) as r:
        return json.loads(r.read().decode())


def _get_bytes(url: str) -> bytes:
    with urllib.request.urlopen(url, timeout=60.0) as r:
        return r.read()


def _toolpath_xy_extents(threemf_bytes: bytes) -> tuple[float, float, float, float]:
    """Walk every G0/G1 in plate_1.gcode after the first M624 and
    return (min_x, max_x, min_y, max_y)."""
    with zipfile.ZipFile(io.BytesIO(threemf_bytes)) as z:
        gcode = z.read("Metadata/plate_1.gcode").decode(errors="replace")
    parts = gcode.split("\nM624 ", 1)
    assert len(parts) >= 2, "no M624 marker in gcode (toolpath did not start)"
    body = parts[1]

    move_re = re.compile(r"^G[01]\s+(.*)$")
    coord_re = re.compile(r"([XY])(-?\d+\.?\d*)")
    min_x = max_x = min_y = max_y = None
    for line in body.splitlines():
        m = move_re.match(line)
        if not m:
            continue
        x = y = None
        for axis, val in coord_re.findall(m.group(1)):
            v = float(val)
            if axis == "X":
                x = v
            elif axis == "Y":
                y = v
        if x is not None:
            min_x = x if min_x is None else min(min_x, x)
            max_x = x if max_x is None else max(max_x, x)
        if y is not None:
            min_y = y if min_y is None else min(min_y, y)
            max_y = y if max_y is None else max(max_y, y)
    assert min_x is not None and min_y is not None, (
        "no G0/G1 X/Y moves found after M624"
    )
    return (min_x, max_x, min_y, max_y)


def test_fixture_08_auto_center_keeps_p2s_project_in_a1m_bounds() -> None:
    """P2S-authored launcher (3 objects at X=181/189/231) sliced on
    A1 mini with auto_center=True must produce a toolpath fully inside
    the 180×180 mm bed."""
    input_path = FIXTURE_DIR / "08" / "reference-launcher-p2s-on-a1m.3mf"
    assert input_path.exists(), f"missing fixture: {input_path}"

    upload = _post_multipart_file(f"{API}/3mf", input_path)
    token = upload["token"]

    slice_resp = _post_json(f"{API}/slice/v2", {
        "input_token": token,
        "machine_id": "GM020",  # Bambu Lab A1 mini 0.4 nozzle
        "process_id": "GP000",  # 0.20mm Standard
        "filament_settings_ids": ["GFSA00_02"],  # Bambu PLA Basic @BBL A1M
        "auto_center": True,
    })
    assert "output_token" in slice_resp, (
        f"slice failed: {slice_resp!r}"
    )

    out_bytes = _get_bytes(f"{API}/3mf/{slice_resp['output_token']}")
    min_x, max_x, min_y, max_y = _toolpath_xy_extents(out_bytes)

    # Strict bounds: every toolpath move stays within the printable area.
    # A small (~1mm) safety margin for purge/skirt is acceptable below 0
    # in the original GUI behaviour, but for this test we expect strict
    # in-bounds since auto_center anchors the model at the bed centre.
    assert 0.0 <= min_x, f"toolpath min X = {min_x} < 0 (off the front-left)"
    assert max_x <= A1M_BED_X, (
        f"toolpath max X = {max_x} > {A1M_BED_X} (off the right edge — "
        f"auto_center did not reseat the model)"
    )
    assert 0.0 <= min_y, f"toolpath min Y = {min_y} < 0"
    assert max_y <= A1M_BED_Y, (
        f"toolpath max Y = {max_y} > {A1M_BED_Y}"
    )


def test_fixture_08_without_auto_center_goes_out_of_bounds() -> None:
    """Negative control: without auto_center, the same project on A1
    mini extrudes past the right edge. This proves the positive test's
    pass condition is meaningful (i.e. it would otherwise fail).

    The slicer happily produces off-bed gcode today — there is no
    validation step that rejects out-of-bounds toolpaths. If a future
    change adds rejection, this test will need to be updated (or
    removed), since there will no longer be an out-of-bounds gcode to
    assert against.
    """
    input_path = FIXTURE_DIR / "08" / "reference-launcher-p2s-on-a1m.3mf"
    assert input_path.exists(), f"missing fixture: {input_path}"

    upload = _post_multipart_file(f"{API}/3mf", input_path)
    token = upload["token"]

    slice_resp = _post_json(f"{API}/slice/v2", {
        "input_token": token,
        "machine_id": "GM020",
        "process_id": "GP000",
        "filament_settings_ids": ["GFSA00_02"],
        "auto_center": False,
    })
    # The slice may still return 200 even with off-bed instances (no
    # validation today). What we care about is that the toolpath
    # *does* go out of bounds, proving the positive test is meaningful.
    assert "output_token" in slice_resp, (
        f"slice failed unexpectedly: {slice_resp!r}"
    )
    out_bytes = _get_bytes(f"{API}/3mf/{slice_resp['output_token']}")
    _, max_x, _, _ = _toolpath_xy_extents(out_bytes)
    assert max_x > A1M_BED_X, (
        f"expected toolpath to exceed bed (max X > {A1M_BED_X}), got "
        f"max X = {max_x}. Either the fixture changed or auto_center "
        f"is leaking through to the False case."
    )
