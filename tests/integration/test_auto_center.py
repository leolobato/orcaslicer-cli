"""Auto-center regression tests.

Two scenarios:

* **Fixture 08 (centred source).** A 3MF authored for a Bambu P2S
  (256mm bed) with three objects whose combined bbox is roughly centred
  on the P2S bed. Sliced as-authored against A1 mini's 180mm bed, the
  rightmost object extrudes off the bed. With ``auto_center=True``, the
  combined bbox is anchored at the A1 mini's bed centre.

* **Off-centre source (generated from fixture 08).** Same model with
  every build item shifted by (+30, 0) on import — the combined bbox
  centroid moves rightward to roughly (165, 132) on P2S. This is the
  case where Path A's bed-size-delta shift (``(target_w−source_w)/2``)
  would still leave the rightmost object off the A1 mini bed, but our
  bbox-recentre primitive (``Model::center_instances_around_point``)
  re-anchors the combined bbox at the new bed centre regardless of
  source layout. Locks in the choice of bbox-recentre over delta-shift.

The headless ``auto_center`` flag bottoms out in libslic3r's
``Model::center_instances_around_point`` + ``ModelObject::ensure_on_bed``
(see ``cpp/src/slice_mode.cpp::auto_center_on_plate``). See CLAUDE.md
for the full GUI/headless behaviour comparison.

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

# Toolpath tolerance: allow ~5mm for skirt/brim overhang past the
# nominal bed edge, and ~20mm of negative X/Y for the wipe purge
# line that A1 mini emits in front-left before printing.
BED_TOLERANCE = 5.0
WIPE_MARGIN = 20.0


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
        "filament_settings_ids": ["GFSA00_02", "GFSA00_02"],  # 2 slots, Bambu PLA Basic @BBL A1M
        "auto_center": True,
    })
    assert "output_token" in slice_resp, (
        f"slice failed: {slice_resp!r}"
    )

    out_bytes = _get_bytes(f"{API}/3mf/{slice_resp['output_token']}")
    min_x, max_x, min_y, max_y = _toolpath_xy_extents(out_bytes)

    # Practical bounds. A1 mini's wipe/purge sequence routinely emits
    # G1 X<0 in front-left of the bed (~X=-13 observed in practice);
    # skirt around an in-bounds model extends ~1mm past its nominal
    # outer edge. Use tolerances that distinguish "essentially in
    # bounds" from "model way off the bed" (without auto_center, max X
    # observed ~207 — 27mm past the edge — vs ~181 with).
    assert min_x >= -WIPE_MARGIN, (
        f"toolpath min X = {min_x} < -{WIPE_MARGIN} (further left than "
        f"A1 mini's wipe purge line)"
    )
    assert max_x <= A1M_BED_X + BED_TOLERANCE, (
        f"toolpath max X = {max_x} > {A1M_BED_X + BED_TOLERANCE} "
        f"(model extends past the right edge — auto_center did not "
        f"reseat the project)"
    )
    assert min_y >= -WIPE_MARGIN, f"toolpath min Y = {min_y} < -{WIPE_MARGIN}"
    assert max_y <= A1M_BED_Y + BED_TOLERANCE, (
        f"toolpath max Y = {max_y} > {A1M_BED_Y + BED_TOLERANCE}"
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
        "filament_settings_ids": ["GFSA00_02", "GFSA00_02"],  # 2 slots, Bambu PLA Basic @BBL A1M
        "auto_center": False,
    })
    # The slice may still return 200 even with off-bed instances (no
    # validation today). What we care about is that the toolpath
    # *does* go out of bounds, proving the positive test is meaningful.
    assert "output_token" in slice_resp, (
        f"slice failed unexpectedly: {slice_resp!r}"
    )
    out_bytes = _get_bytes(f"{API}/3mf/{slice_resp['output_token']}")
    _, max_x, _, max_y = _toolpath_xy_extents(out_bytes)
    # Sharper threshold than (A1M_BED_X + BED_TOLERANCE): the authored
    # P2S layout puts the model edge well past the A1 mini bed (max X
    # ~207 observed). Anything within tolerance would mean auto_center
    # leaked through to the False case.
    assert max_x > A1M_BED_X + BED_TOLERANCE, (
        f"expected toolpath to clearly exceed bed (max X > "
        f"{A1M_BED_X + BED_TOLERANCE}), got max X = {max_x}. Either the "
        f"fixture changed or auto_center is leaking through to the "
        f"False case."
    )


def _shift_3mf_build_items(src: Path, dst: Path, dx: float, dy: float) -> None:
    """Copy ``src`` to ``dst``, adding ``(dx, dy)`` to every build item's
    XY translation.

    The 3MF stores each instance under ``3D/3dmodel.model`` as
    ``<item ... transform="m11 m21 m31 m12 m22 m32 m13 m23 m33 tx ty tz"/>``.
    The trailing three floats are the world-space translation; we shift
    only X and Y, leaving Z (drop-to-bed height) untouched.
    """
    item_re = re.compile(r'(<item\s[^>]*\btransform=")([^"]+)(")')

    def _shift(transform: str) -> str:
        vals = transform.split()
        if len(vals) != 12:
            return transform
        vals[9] = f"{float(vals[9]) + dx}"
        vals[10] = f"{float(vals[10]) + dy}"
        return " ".join(vals)

    with zipfile.ZipFile(src) as zin, zipfile.ZipFile(
        dst, "w", zipfile.ZIP_DEFLATED
    ) as zout:
        for entry in zin.infolist():
            data = zin.read(entry.filename)
            if entry.filename == "3D/3dmodel.model":
                xml = data.decode()
                xml = item_re.sub(
                    lambda m: m.group(1) + _shift(m.group(2)) + m.group(3),
                    xml,
                )
                data = xml.encode()
            zout.writestr(entry, data)


@pytest.fixture(scope="module")
def offcenter_3mf(tmp_path_factory) -> Path:
    """Generate an off-centre derivative of fixture 08.

    Shifts every build item by (+30, 0). The combined bbox centroid moves
    from roughly (135, 132) on a P2S 256mm bed (near-centre) to roughly
    (165, 132) — clearly off-centre rightward but still on P2S. Used to
    exercise the bbox-recentre vs delta-shift divergence.
    """
    src = FIXTURE_DIR / "08" / "reference-launcher-p2s-on-a1m.3mf"
    assert src.exists(), f"missing fixture 08 source: {src}"
    dst = tmp_path_factory.mktemp("offcenter") / "launcher-p2s-offcenter.3mf"
    _shift_3mf_build_items(src, dst, dx=30.0, dy=0.0)
    return dst


def test_offcenter_auto_center_keeps_in_bounds(offcenter_3mf: Path) -> None:
    """Off-centre P2S source retargeted to A1 mini with auto_center=True
    must land in bounds.

    The combined bbox centroid is at ~(165, 132) on P2S — off-centre by
    roughly +30 in X relative to the bed centre. This is exactly the
    case where Path A's mechanism (uniform shift by
    ``(target_w - source_w)/2`` = -38, -38) would diverge from our
    bbox-recentre: delta-shift would put the rightmost object's mesh
    edge at roughly X=210, ~30mm off A1 mini's 180mm bed. Our
    ``Model::center_instances_around_point`` instead anchors the
    combined bbox at the A1 mini's bed centre (90, 90), keeping the
    model in bounds regardless of the source's offset on its authoring
    bed. Locks in the bbox-recentre choice (see CLAUDE.md auto_center
    section).
    """
    upload = _post_multipart_file(f"{API}/3mf", offcenter_3mf)
    token = upload["token"]

    slice_resp = _post_json(f"{API}/slice/v2", {
        "input_token": token,
        "machine_id": "GM020",
        "process_id": "GP000",
        "filament_settings_ids": ["GFSA00_02", "GFSA00_02"],
        "auto_center": True,
    })
    assert "output_token" in slice_resp, f"slice failed: {slice_resp!r}"

    out_bytes = _get_bytes(f"{API}/3mf/{slice_resp['output_token']}")
    min_x, max_x, min_y, max_y = _toolpath_xy_extents(out_bytes)

    assert min_x >= -WIPE_MARGIN, (
        f"toolpath min X = {min_x} < -{WIPE_MARGIN}"
    )
    assert max_x <= A1M_BED_X + BED_TOLERANCE, (
        f"toolpath max X = {max_x} > {A1M_BED_X + BED_TOLERANCE}: "
        f"bbox-recentre did not anchor the off-centre source on the "
        f"target bed (delta-shift fallback would land here)"
    )
    assert min_y >= -WIPE_MARGIN, f"toolpath min Y = {min_y}"
    assert max_y <= A1M_BED_Y + BED_TOLERANCE, (
        f"toolpath max Y = {max_y}"
    )


def test_offcenter_without_auto_center_extrudes_off_bed(
    offcenter_3mf: Path,
) -> None:
    """Negative control for the off-centre case: with auto_center=False,
    the model retains its authored P2S coordinates and extrudes well
    past the A1 mini bed. Confirms the positive test catches a real
    bbox-recentre signal rather than a coincidence.
    """
    upload = _post_multipart_file(f"{API}/3mf", offcenter_3mf)
    token = upload["token"]

    slice_resp = _post_json(f"{API}/slice/v2", {
        "input_token": token,
        "machine_id": "GM020",
        "process_id": "GP000",
        "filament_settings_ids": ["GFSA00_02", "GFSA00_02"],
        "auto_center": False,
    })
    assert "output_token" in slice_resp, (
        f"slice failed unexpectedly: {slice_resp!r}"
    )
    out_bytes = _get_bytes(f"{API}/3mf/{slice_resp['output_token']}")
    _, max_x, _, _ = _toolpath_xy_extents(out_bytes)
    assert max_x > A1M_BED_X + BED_TOLERANCE, (
        f"expected toolpath to clearly exceed bed (max X > "
        f"{A1M_BED_X + BED_TOLERANCE}), got max X = {max_x}"
    )
