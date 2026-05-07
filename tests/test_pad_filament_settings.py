"""Tests for sparse-AMS-slot padding of `filament_settings_ids`.

The headless wrapper currently builds `filament_settings_ids` positionally
(one entry per authored filament). For sliced 3MFs that authored filaments
on sparse AMS slots (e.g. only slot 1 used), libslic3r's per-filament
extruder lookup walks `max(slot)+1` and fails when the array is shorter.
`pad_filament_settings_for_sparse_3mf` re-shapes the caller's array to
match `max(authored_slot)+1`.
"""

import io
import unittest
import zipfile

from app.slicer import pad_filament_settings_for_sparse_3mf


def _make_sliced_3mf(filament_ids_in_slice_info: list[int]) -> bytes:
    """Build a minimal 3MF that `parse_inspect_data` will recognise as sliced.

    `filament_ids_in_slice_info` are the `<filament id="...">` ids written
    into `Metadata/slice_info.config` (1-based; inspect.py converts to
    0-based slots via `id - 1`).
    """
    filaments_xml = "".join(
        f'<filament id="{fid}" type="PLA" color="#FFFFFF" used_m="1.0" used_g="1.0" tray_info_idx="GFA00"/>'
        for fid in filament_ids_in_slice_info
    )
    slice_info_xml = (
        '<?xml version="1.0"?>'
        "<config>"
        '<plate>'
        '<metadata key="index" value="1"/>'
        f'{filaments_xml}'
        '</plate>'
        "</config>"
    )
    model_xml = (
        '<?xml version="1.0"?>'
        '<model xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02">'
        '<resources>'
        '<object id="1" type="model"><mesh><vertices/><triangles/></mesh></object>'
        '</resources>'
        '<build><item objectid="1" transform="1 0 0 0 1 0 0 0 1 0 0 0"/></build>'
        '</model>'
    )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("[Content_Types].xml", "")
        zf.writestr("3D/3dmodel.model", model_xml)
        zf.writestr("Metadata/slice_info.config", slice_info_xml)
    return buf.getvalue()


class PadFilamentSettingsForSparseThreeMfTests(unittest.TestCase):
    def test_pads_for_sparse_slot_1_only(self) -> None:
        # Mirrors the `Gravity Broom Holder.3mf` repro: sliced 3MF with
        # exactly one authored filament, on AMS slot 1 (id=2 in slice_info,
        # 0-based slot=1). Caller (gateway) sends positional length 1; we
        # need to pad to length 2 and place the id at index 1.
        bytes_3mf = _make_sliced_3mf(filament_ids_in_slice_info=[2])

        padded = pad_filament_settings_for_sparse_3mf(["GFSA04_10"], bytes_3mf)

        self.assertEqual(padded, ["GFSA04_10", "GFSA04_10"])

    def test_no_change_when_dense_from_zero(self) -> None:
        # Dense from slot 0 — the positional array already matches
        # `max(slot)+1`, so this must be a no-op.
        bytes_3mf = _make_sliced_3mf(filament_ids_in_slice_info=[1])

        padded = pad_filament_settings_for_sparse_3mf(["GFSA04_10"], bytes_3mf)

        self.assertEqual(padded, ["GFSA04_10"])

    def test_no_change_when_multifilament_dense(self) -> None:
        # Two authored filaments on slots 0 and 1, caller sends two ids in
        # the same order — no padding needed.
        bytes_3mf = _make_sliced_3mf(filament_ids_in_slice_info=[1, 2])

        padded = pad_filament_settings_for_sparse_3mf(
            ["GFSA00", "GFSL01"], bytes_3mf,
        )

        self.assertEqual(padded, ["GFSA00", "GFSL01"])

    def test_pads_higher_sparse_slot(self) -> None:
        # Single filament on slot 3 (id=4) — pad to length 4 and place at
        # index 3. Filler positions are unused by toolpath but must be
        # valid id references.
        bytes_3mf = _make_sliced_3mf(filament_ids_in_slice_info=[4])

        padded = pad_filament_settings_for_sparse_3mf(["GFSA04_10"], bytes_3mf)

        self.assertEqual(len(padded), 4)
        self.assertEqual(padded[3], "GFSA04_10")
        # Filler positions all hold the caller's first id.
        self.assertEqual(padded[0], "GFSA04_10")
        self.assertEqual(padded[1], "GFSA04_10")
        self.assertEqual(padded[2], "GFSA04_10")

    def test_returns_input_when_3mf_is_unparseable(self) -> None:
        # Best-effort: if the inspector can't read the bytes, the wrapper
        # should not crash — pass the array through unchanged so the
        # binary's own error path can run.
        padded = pad_filament_settings_for_sparse_3mf(
            ["GFSA04_10"], b"not a zip",
        )

        self.assertEqual(padded, ["GFSA04_10"])

    def test_returns_input_when_filament_count_mismatches(self) -> None:
        # Caller sent 2 ids but 3MF only authors 1 filament — we can't
        # safely pair without a trusted mapping, so leave the array alone
        # and let the binary's own validation run.
        bytes_3mf = _make_sliced_3mf(filament_ids_in_slice_info=[2])

        padded = pad_filament_settings_for_sparse_3mf(
            ["GFSA00", "GFSL01"], bytes_3mf,
        )

        self.assertEqual(padded, ["GFSA00", "GFSL01"])

    def test_returns_empty_input_unchanged(self) -> None:
        bytes_3mf = _make_sliced_3mf(filament_ids_in_slice_info=[1])

        padded = pad_filament_settings_for_sparse_3mf([], bytes_3mf)

        self.assertEqual(padded, [])


if __name__ == "__main__":
    unittest.main()
