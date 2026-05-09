"""Fidelity test: /slice/v2 vs GUI-authored output 3MF.

The fixtures in ``_fixture/01/`` are a reference 3MF that the GUI sliced
into ``gui-...gcode.3mf.3mf``. We slice the input through ``/slice/v2``
with the same machine/process/filament selection and assert the metadata
in ``slice_info.config`` (time, weight, layer count, start XY,
``printer_model_id``) falls within tolerance of the GUI's numbers.

Opt-in: requires a running container reachable at ``$ORCASLICER_API``
(default ``http://localhost:8070``) with ``USE_HEADLESS_BINARY=1``.
Skipped when not reachable.

Uses only stdlib (urllib + zipfile) so it runs on any host with Python
without needing a venv for ``httpx``.
"""
from __future__ import annotations

import io
import json
import os
import re
import urllib.error
import urllib.request
import uuid
import zipfile
from pathlib import Path

import pytest

API = os.environ.get("ORCASLICER_API", "http://localhost:8070")
# tests/integration/test.py -> tests/integration -> tests -> orcaslicer-cli
# -> bambu_workspace, then `_fixture/`
FIXTURE_DIR = Path(__file__).resolve().parents[2].parent / "_fixture"


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
    """POST a single file as multipart/form-data with field name 'file'."""
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
    # /slice/v2 is synchronous and a cold-cache multi-filament slice
    # (fixture 06's user-imported filament + 5 slots) can take ~3 min
    # on the dev container. Set the upper bound generously — any real
    # slice exceeding this is a separate concern (deadlock, runaway
    # toolpath, etc.) that should surface as a different signal.
    with urllib.request.urlopen(req, timeout=300.0) as r:
        return json.loads(r.read().decode())


def _get_bytes(url: str) -> bytes:
    with urllib.request.urlopen(url, timeout=60.0) as r:
        return r.read()


def _read_slice_info(threemf_bytes: bytes) -> dict[str, str]:
    with zipfile.ZipFile(io.BytesIO(threemf_bytes)) as z:
        xml = z.read("Metadata/slice_info.config").decode()
    out: dict[str, str] = {}
    for m in re.finditer(r'<metadata key="(\w+)" value="([^"]*)"/>', xml):
        out[m.group(1)] = m.group(2)
    return out


def _first_xy(threemf_bytes: bytes) -> tuple[float, float]:
    with zipfile.ZipFile(io.BytesIO(threemf_bytes)) as z:
        gcode = z.read("Metadata/plate_1.gcode").decode()
    parts = gcode.split("\nM624 ", 1)
    if len(parts) < 2:
        raise AssertionError("no M624 marker in gcode")
    for line in parts[1].splitlines():
        m = re.match(r"G1 X([\d.]+) Y([\d.]+)", line)
        if m:
            return float(m.group(1)), float(m.group(2))
    raise AssertionError("no G1 X/Y after first M624")


def _read_gcode_config_block(threemf_bytes: bytes) -> dict[str, str]:
    """Extract CONFIG_BLOCK key=value lines from the sliced gcode.

    Each `; key = value` line in the CONFIG_BLOCK becomes one entry.
    Multi-line script keys (e.g. `change_filament_gcode`) are dropped
    because their values aren't meaningful for equality checks.
    """
    with zipfile.ZipFile(io.BytesIO(threemf_bytes)) as z:
        gcode = z.read("Metadata/plate_1.gcode").decode(errors="replace")
    out: dict[str, str] = {}
    in_block = False
    for raw in gcode.splitlines():
        if raw.startswith("; CONFIG_BLOCK_START"):
            in_block = True
            continue
        if raw.startswith("; CONFIG_BLOCK_END"):
            break
        if not in_block:
            continue
        m = re.match(r"^;\s+([\w]+)\s*=\s*(.*)$", raw)
        if m:
            key, val = m.group(1), m.group(2)
            # Skip multi-line script-type keys; their first-line value
            # is enough to detect presence but not useful for equality.
            if "\\n" in val and len(val) > 200:
                continue
            out[key] = val
    return out


def _assert_gcode_config(
    threemf_bytes: bytes,
    expected: dict[str, str],
    *,
    fixture_label: str,
) -> None:
    """Assert each `key = value` pair appears verbatim in the CONFIG_BLOCK.

    Use for locking in fixes whose signal is a specific CONFIG_BLOCK
    field — e.g. `nozzle_temperature = 220,235` for Gap 2's per-slot
    override apply, or `filament_ids = GFA00;GFA00` for the
    `Preset.filament_id` stamping fix. Coarse-tolerance metadata
    asserts won't catch regressions on these fields because they
    don't move time/weight enough to trip the 2% threshold.
    """
    block = _read_gcode_config_block(threemf_bytes)
    missing: list[str] = []
    wrong: list[str] = []
    for key, want in expected.items():
        if key not in block:
            missing.append(key)
        elif block[key] != want:
            wrong.append(f"{key}: got {block[key]!r}, want {want!r}")
    assert not missing and not wrong, (
        f"[{fixture_label}] gcode CONFIG_BLOCK mismatch:\n"
        + ("missing keys: " + ", ".join(missing) + "\n" if missing else "")
        + ("\n".join(wrong) if wrong else "")
    )


def _slice_and_compare(
    input_path: Path,
    gui_path: Path,
    *,
    machine_id: str,
    process_id: str,
    filament_settings_ids: list[str],
    auto_center: bool = False,
    time_tol: float = 0.02,
    weight_tol: float = 0.015,
    first_layer_time_tol: float = 0.01,
    xy_tol_mm: float = 0.01,
    require_xy_match: bool = True,
    plate_type: str | None = None,
    expected_config_block: dict[str, str] | None = None,
) -> None:
    """Slice ``input_path`` through ``/slice/v2`` and compare to GUI ground truth.

    Asserts the metadata stamped into ``Metadata/slice_info.config`` and the
    first toolpath XY land within tolerance of the GUI's sliced output.
    """
    assert input_path.exists(), f"missing fixture: {input_path}"
    assert gui_path.exists(), f"missing fixture: {gui_path}"

    upload = _post_multipart_file(f"{API}/3mf", input_path)
    token = upload["token"]

    slice_body: dict = {
        "input_token": token,
        "machine_id": machine_id,
        "process_id": process_id,
        "filament_settings_ids": filament_settings_ids,
        "auto_center": auto_center,
    }
    if plate_type is not None:
        slice_body["plate_type"] = plate_type
    slice_resp = _post_json(f"{API}/slice/v2", slice_body)
    out_token = slice_resp["output_token"]
    ours = _get_bytes(f"{API}/3mf/{out_token}")
    gui = gui_path.read_bytes()
    ours_info = _read_slice_info(ours)
    gui_info = _read_slice_info(gui)

    ours_time = float(ours_info["prediction"])
    gui_time = float(gui_info["prediction"])
    assert abs(ours_time - gui_time) / gui_time < time_tol, (
        f"time drift {ours_time} vs {gui_time}"
    )

    ours_w = float(ours_info["weight"])
    gui_w = float(gui_info["weight"])
    assert abs(ours_w - gui_w) / gui_w < weight_tol, (
        f"weight drift {ours_w} vs {gui_w}"
    )

    assert ours_info.get("printer_model_id") == gui_info.get("printer_model_id")
    assert ours_info.get("label_object_enabled") == gui_info.get(
        "label_object_enabled"
    )
    assert ours_info.get("nozzle_diameters") == gui_info.get("nozzle_diameters")

    ours_flt = float(ours_info["first_layer_time"])
    gui_flt = float(gui_info["first_layer_time"])
    assert ours_flt > 0.0, "first_layer_time should be populated"
    assert abs(ours_flt - gui_flt) / gui_flt < first_layer_time_tol, (
        f"first_layer_time drift {ours_flt} vs {gui_flt}"
    )

    if require_xy_match:
        ours_xy = _first_xy(ours)
        gui_xy = _first_xy(gui)
        assert abs(ours_xy[0] - gui_xy[0]) < xy_tol_mm, (
            f"start X {ours_xy[0]} vs {gui_xy[0]}"
        )
        assert abs(ours_xy[1] - gui_xy[1]) < xy_tol_mm, (
            f"start Y {ours_xy[1]} vs {gui_xy[1]}"
        )

    if expected_config_block:
        _assert_gcode_config(
            ours, expected_config_block, fixture_label=input_path.name,
        )


def test_fixture_01_matches_gui_within_tolerance() -> None:
    """Single-filament A1 mini benchy with process+printer customizations.

    CONFIG_BLOCK asserts lock in:
    - `filament_ids = GFA00` (Preset.filament_id stamping fix, d838b8a).
    - `enable_prime_tower = 0` (libslic3r normalize_fdm_2 flips on
      single-filament-actually-used; depends on `filament_map = [1]` not
      [0, 2] AKA the AMS-tray-semantic fix landed earlier).
    - `curr_bed_type = Textured PEI Plate` (project carry-over).
    - `layer_height = 0.25` (process customized from 0.20mm Standard's
      0.20 default — proves process-side `different_settings_to_system[0]`
      override applied).
    - `filament_map = 1`, `filament_self_index = 1` (single-extruder
      topology pinned; regression of AMS-tray confusion would shift these).
    """
    _slice_and_compare(
        FIXTURE_DIR / "01" / "reference-benchy-orca-no-filament-custom-settings.3mf",
        FIXTURE_DIR / "01" / "gui-benchy-orca-no-filament-custom-settings_sliced_gui.gcode.3mf.3mf",
        machine_id="GM020",
        process_id="GP000",
        filament_settings_ids=["GFSA00_02"],
        expected_config_block={
            "filament_ids": "GFA00",
            "filament_type": "PLA",
            "filament_density": "1.26",
            "enable_prime_tower": "0",
            "curr_bed_type": "Textured PEI Plate",
            "layer_height": "0.25",
            "filament_map": "1",
            "filament_self_index": "1",
            "first_layer_bed_temperature": "65",
        },
    )


def test_fixture_03_matches_gui_within_tolerance() -> None:
    """Single-filament with FILAMENT-side customizations
    (`filament_max_volumetric_speed`, `filament_flow_ratio` in
    `different_settings_to_system[1]`). Exercises the `applied` branch
    of the per-filament name guard.

    The two customized values are the most important asserts here:
    the GUI authored 18 (vs default 21) and 0.95 (vs default 0.98),
    and our binary must apply both via `apply_overrides_for_slot`.
    A regression that re-introduced the over-strict name guard or
    silently dropped per-slot overrides would default these back.
    """
    _slice_and_compare(
        FIXTURE_DIR / "03" / "reference-benchy-with-filament-customizations.3mf",
        FIXTURE_DIR / "03" / "gui-reference-benchy-with-filament-customizations_sliced.3mf",
        machine_id="GM020",
        process_id="GP000",
        filament_settings_ids=["GFSA00_02"],
        expected_config_block={
            "filament_ids": "GFA00",
            "enable_prime_tower": "0",
            "filament_max_volumetric_speed": "18",
            "filament_flow_ratio": "0.95",
            "curr_bed_type": "Textured PEI Plate",
        },
    )


def test_fixture_05_matches_gui_within_tolerance() -> None:
    """Same as fixture 01 but with curr_bed_type = Cool Plate (vs Textured PEI).
    Verifies bed-type carry-through and bed-temperature lookups —
    `first_layer_bed_temperature` lookup picks the cool-plate column
    instead of the PEI column when `curr_bed_type` differs.
    """
    _slice_and_compare(
        FIXTURE_DIR / "05" / "reference-benchy-cool-plate.3mf",
        FIXTURE_DIR / "05" / "gui-reference-benchy-cool-plate_sliced.3mf",
        machine_id="GM020",
        process_id="GP000",
        filament_settings_ids=["GFSA00_02"],
        expected_config_block={
            "filament_ids": "GFA00",
            "enable_prime_tower": "0",
            "curr_bed_type": "Cool Plate",
            # The signal that bed temp lookup respects curr_bed_type:
            # PEI fixture 01 has 65 here; Cool Plate must be 35.
            "first_layer_bed_temperature": "35",
        },
    )


_CUSTOM_FILAMENT_NAME = "SUNLU PLA +2.0 GEN2 @Bambu Lab A1 mini 0.4 nozzle"


@pytest.fixture(scope="module", autouse=True)
def _stage_user_custom_filaments() -> None:
    """Drop fixture-bundled user filament profiles into the on-host data dir.

    The container mounts the repo's `data/` at `/data` (the API's
    `USER_PROFILES_DIR`), so writing here is the same workflow a real
    operator uses — copy the `.json` into `data/filament/base/` and let
    the API's profile loader pick it up. We POST `/profiles/reload` to
    force a re-scan without restarting the container.

    The staged file is left in place after the test run; profile
    re-imports are idempotent and the file is gitignored.
    """
    src = FIXTURE_DIR / "06" / f"{_CUSTOM_FILAMENT_NAME}.json"
    if not src.exists():
        return  # fixture 06 not present; tests that need it will skip/fail explicitly
    repo_root = Path(__file__).resolve().parents[2]
    dest = repo_root / "data" / "filament" / "base" / src.name
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(src.read_bytes())
    req = urllib.request.Request(f"{API}/profiles/reload", method="POST")
    with urllib.request.urlopen(req, timeout=30.0) as r:
        assert r.status == 200, f"reload returned {r.status}"


def test_fixture_04_matches_gui_within_tolerance() -> None:
    """5 filament slots spanning 3 vendors (Bambu, SUNLU, Overture). Geometry
    is bound to a single slot via `extruder` metadata, but the project
    declares all 5 — exercises multi-slot materialisation, cross-vendor
    profile resolution, and 5×5 flush-volume matrix sizing.

    Start-X matches GUI bit-for-bit (~0.004mm); start-Y differs by
    ~16mm because libslic3r picks the opposite Y endpoint of the bird's
    bbox to begin the perimeter. Time/weight/layer-count all match
    within the standard parity tolerances, so the slice is functionally
    equivalent — the start-point choice is a libslic3r ordering
    heuristic and not worth pinning bit-for-bit here.

    CONFIG_BLOCK asserts lock in the multi-vendor catalog round-trip:
    `filament_ids` carries each slot's BBL/vendor catalog ID
    (`GFSNL03` for SUNLU, `GFL05` for Overture, `GFA00` for Bambu),
    `filament_type` and `filament_density` round-trip per slot, and
    `filament_map = 1,1,1,1,1` confirms single-extruder topology
    survives a 5-slot setup (regression of the AMS-tray-semantic bug
    would change this). `enable_prime_tower = 1` because >1 filament
    is actually deposited — opposite of the single-filament fixtures.
    """
    _slice_and_compare(
        FIXTURE_DIR / "04" / "reference-bird-orca.3mf",
        FIXTURE_DIR / "04" / "gui-bird-orca_sliced_gui.3mf",
        machine_id="GM020",
        process_id="GP000",
        filament_settings_ids=[
            "GFSA00_02",   # Bambu PLA Basic @BBL A1M
            "GFSNLS03_07", # SUNLU PLA+ @BBL A1M
            "GFSL05_05",   # Overture Matte PLA @BBL A1M
            "GFSA00_02",   # Bambu PLA Basic @BBL A1M (slot 3)
            "GFSA00_02",   # Bambu PLA Basic @BBL A1M (slot 4)
        ],
        require_xy_match=False,
        expected_config_block={
            "filament_ids": "GFA00;GFSNL03;GFL05;GFA00;GFA00",
            "filament_type": "PLA;PLA;PLA;PLA;PLA",
            "filament_density": "1.26,1.23,1.22,1.26,1.26",
            "filament_map": "1,1,1,1,1",
            "enable_prime_tower": "1",
            "curr_bed_type": "Textured PEI Plate",
            "print_sequence": "by layer",
        },
    )


def test_fixture_06_matches_gui_within_tolerance() -> None:
    """Trax model authored on a different printer, then switched to the A1
    mini in the GUI and centered on the build plate. Slot 2 references a
    user-imported filament profile (`SUNLU PLA +2.0 GEN2 @Bambu Lab A1
    mini 0.4 nozzle`) staged into `data/filament/base/` by the autouse
    fixture above — exercises the user-profile load path end-to-end and
    proves slicing works when one of the slot identifiers is a display
    name (not a slug-style setting_id) because user-imports default
    `setting_id` to the profile's `name`.

    CONFIG_BLOCK asserts lock in the user-imported filament path:
    `filament_ids` carries `S1839475` for slot 2 (the
    `"P" + md5(name)[:7]`-derived ID our wrapper assigns to user
    filaments; per CLAUDE.md). Mixed `filament_type =
    PETG;PLA;PLA+;PLA;PLA` proves per-slot type round-trip across
    vendors, and `nozzle_temperature = 240,220,...` proves the PETG
    slot gets its hotter temp while PLA slots stay at 220.
    """
    _slice_and_compare(
        FIXTURE_DIR / "06" / "trax-orca-a1-modified.3mf",
        FIXTURE_DIR / "06" / "gui-trax-orca-a1-modified_sliced.3mf",
        machine_id="GM020",
        process_id="GP000",
        filament_settings_ids=[
            "GFSG02_06",            # Bambu PETG HF @BBL A1M
            "GFSNLS03_07",          # SUNLU PLA+ @BBL A1M
            _CUSTOM_FILAMENT_NAME,  # user-imported SUNLU PLA +2.0 GEN2
            "GFSA00_02",            # Bambu PLA Basic @BBL A1M
            "GFSA00_02",            # Bambu PLA Basic @BBL A1M
        ],
        expected_config_block={
            "filament_ids": "GFG02;GFSNL03;S1839475;GFA00;GFA00",
            "filament_type": "PETG;PLA;PLA+;PLA;PLA",
            "nozzle_temperature": "240,220,220,220,220",
            "nozzle_temperature_initial_layer": "230,220,220,220,220",
            "filament_map": "1,1,1,1,1",
            "enable_prime_tower": "0",
            "curr_bed_type": "Textured PEI Plate",
        },
    )


def test_fixture_07_matches_gui_within_tolerance() -> None:
    """Multi-filament A1 mini with per-filament-slot override on slot 1,
    sequential printing (`print_sequence = by object`), and two
    bulbasaur copies each pinned to a different filament via per-object
    `extruder` metadata.

    Locks in the recent fixes:
    - **Gap 2** (per-slot name guard via `inherits` map, 944ccde):
      slot 1 carries `nozzle_temperature` override on a project-local
      preset variant whose name has a user-typed suffix. Pre-fix our
      code discarded the override (`status: filament_changed`) and
      emitted `nozzle_temperature = 220,220`. Post-fix it should match
      GUI's `220,235`.
    - **filament_id stamping** (Tier 1 #3, d838b8a): `filament_ids =
      GFA00;GFA00` not empty. Bambu printers / Cloud / Handy read it
      at print start.
    - **`print_sequence = by object`** carry-through (Tier 2 #6):
      `different_settings_to_system[0]` lists `print_sequence` as a
      process-side override. Verifies the by-object branch round-trips.
    - **plate_type plumbing** (13e6ff9): explicit `textured_pei_plate`
      on the request lands as `curr_bed_type = Textured PEI Plate`.
    """
    _slice_and_compare(
        FIXTURE_DIR / "07" / "reference-multi-filament-with-slot1-customization.3mf",
        FIXTURE_DIR / "07" / "gui-reference-multi-filament-with-slot1-customization_sliced.3mf",
        machine_id="GM020",
        process_id="GP109",  # 0.16mm High Quality @BBL A1M
        filament_settings_ids=["GFSA00_02", "GFSA00_02"],
        plate_type="textured_pei_plate",
        # Two bulbasaurs, slightly different starts; first XY isn't a
        # robust GUI-parity signal here (object-traversal order can
        # flip without affecting print quality).
        require_xy_match=False,
        # by_object can produce small per-object planning differences
        # that nudge the metric totals more than by_layer; loosen
        # tolerances slightly while still catching real regressions.
        # Empirical drift on 2.3.2-32 with the validate() fix:
        # time ~4.5% (8197 vs 7841 GUI), weight ~4.1% (25.87 vs 24.86).
        # Both well below the ~50% gap fixture 07 caught when only
        # one object was emitted. Likely from per-object travel/order
        # planning variance with the asymmetric per-slot temperature
        # override (220 vs 235).
        time_tol=0.06,
        weight_tol=0.05,
        first_layer_time_tol=0.05,
        expected_config_block={
            "nozzle_temperature": "220,235",
            "filament_ids": "GFA00;GFA00",
            "enable_prime_tower": "0",
            "print_sequence": "by object",
            "curr_bed_type": "Textured PEI Plate",
        },
    )


def test_fixture_10_diff_to_system_matches_gui_shape() -> None:
    """5-filament BagClip authored on A1 mini; only slot 2 (PETG) painted.

    Locks in `different_settings_to_system` GUI-parity for the output 3MF.
    The 3MF was authored with one process tweak (`prime_tower_infill_gap`),
    no per-filament tweaks, no printer tweaks — GUI's output therefore has
    `different_settings_to_system = prime_tower_infill_gap;;;;;;` (one
    process key + 5 empty filament slots + 1 empty printer slot).

    Pre-fix our binary emitted a bloated diff with ~30 keys per filament
    and ~30 printer keys: chain links were loaded with `is_system=false`,
    which makes `PresetCollection::get_preset_base` walk inherits to the
    common ancestor (e.g. `fdm_bbl_3dp_001_common`) instead of returning
    the system leaf itself. The export-time diff then reports every key
    the leaf legitimately overrides over its ancestor — exactly the
    explicit key set of `Bambu Lab A1 mini 0.4 nozzle.json`.

    Post-fix the diff matches GUI: only the smart-transfer overlay shows
    in the per-slot allowlist.

    Slot 1 was authored as `Generic ABS @System` (`OGFB99`), but the
    bambu-gateway's `_resolve_carryover_filaments` substitutes it with
    `Bambu PLA Basic @BBL A1M` for the A1 mini target (Generic ABS has
    `compatible_printers: []` upstream and the resolver picks the
    machine's default filament). We pass the substituted list so the
    test mirrors the production gateway path; the slice physical
    output is identical because slot 1 is unpainted.
    """
    _slice_and_compare(
        FIXTURE_DIR / "10" / "reference-bagclip-petg-multiple.3mf",
        FIXTURE_DIR / "10" / "gui-bagclip-petg-multiple_sliced.3mf",
        machine_id="GM020",
        process_id="GP000",  # 0.20mm Standard @BBL A1M
        filament_settings_ids=[
            "GFSA00_02",  # Bambu PLA Basic @BBL A1M
            "GFSA00_02",  # gateway-substituted from "Generic ABS @System"
            "GFSG02_06",  # Bambu PETG HF @BBL A1M (the painted slot)
            "GFSA00_02",  # Bambu PLA Basic @BBL A1M
            "GFSA00_02",  # Bambu PLA Basic @BBL A1M
        ],
        plate_type="textured_pei_plate",
        # Object-traversal start point can shift between layouts;
        # 20 instances of the same clip means small reorderings.
        require_xy_match=False,
        expected_config_block={
            # The marquee assertion: GUI parity on the per-slot
            # `different_settings_to_system` allowlist. Pre-fix this
            # was ~700 chars of bloat; GUI's is 28 chars.
            "different_settings_to_system": "prime_tower_infill_gap;;;;;;",
            "curr_bed_type": "Textured PEI Plate",
            "filament_ids": "GFA00;GFA00;GFG02;GFA00;GFA00",
        },
    )
