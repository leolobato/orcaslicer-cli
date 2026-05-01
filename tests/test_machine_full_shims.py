"""Tests for the BBL ``machine_full`` shim writer.

OrcaSlicer's CLI reads ``printer_model_id`` from ``BBL/machine_full/{name}.json``
at slice time. The AppImage we extract doesn't ship that directory, so without
the shim the slice output's ``slice_info.config`` ends up with
``printer_model_id=""``. ``_write_bbl_machine_full_shims`` materializes minimal
``{"model_id": ...}`` files for every BBL parent machine profile.
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from unittest import mock

from app import profiles
from tests._profile_test_helpers import reset_profiles_state


class MachineFullShimsTests(unittest.TestCase):
    def setUp(self) -> None:
        reset_profiles_state()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        # Mirror the runtime layout: shims land under
        # ``ORCA_RESOURCES_DIR/profiles/BBL/`` (the binary's resources root),
        # not ``PROFILES_DIR``.
        self.bbl_dir = os.path.join(self.tmp.name, "profiles", "BBL")
        os.makedirs(self.bbl_dir)
        self._patcher = mock.patch.object(
            profiles, "ORCA_RESOURCES_DIR", self.tmp.name,
        )
        self._patcher.start()
        self.addCleanup(self._patcher.stop)

    def _index(self, vendor: str, name: str, data: dict) -> None:
        merged = {"name": name, **data}
        profiles._index_profile(
            profiles._profile_key(vendor, name), merged, "machine", vendor,
        )

    def test_writes_shim_for_each_bbl_parent(self) -> None:
        self._index("BBL", "Bambu Lab A1 mini", {"model_id": "N1"})
        self._index("BBL", "Bambu Lab P1S", {"model_id": "C12"})

        profiles._write_bbl_machine_full_shims()

        a1 = os.path.join(
            self.bbl_dir, "machine_full", "Bambu Lab A1 mini.json",
        )
        p1s = os.path.join(
            self.bbl_dir, "machine_full", "Bambu Lab P1S.json",
        )
        self.assertEqual(json.load(open(a1)), {"model_id": "N1"})
        self.assertEqual(json.load(open(p1s)), {"model_id": "C12"})

    def test_skips_child_profiles(self) -> None:
        # Child machine profiles inherit from a parent — they don't carry the
        # canonical model_id and the shim filename would clash.
        self._index("BBL", "Bambu Lab A1 mini", {"model_id": "N1"})
        self._index(
            "BBL", "Bambu Lab A1 mini 0.4 nozzle",
            {"inherits": "Bambu Lab A1 mini", "model_id": "N1"},
        )

        profiles._write_bbl_machine_full_shims()

        files = os.listdir(os.path.join(self.bbl_dir, "machine_full"))
        self.assertEqual(files, ["Bambu Lab A1 mini.json"])

    def test_skips_non_bbl_vendors(self) -> None:
        self._index("Creality", "Ender-3", {"model_id": "E3"})

        profiles._write_bbl_machine_full_shims()

        machine_full = os.path.join(self.bbl_dir, "machine_full")
        # Directory is created but stays empty.
        self.assertEqual(os.listdir(machine_full), [])

    def test_idempotent_when_content_matches(self) -> None:
        self._index("BBL", "Bambu Lab A1 mini", {"model_id": "N1"})
        profiles._write_bbl_machine_full_shims()
        path = os.path.join(
            self.bbl_dir, "machine_full", "Bambu Lab A1 mini.json",
        )
        first_mtime = os.stat(path).st_mtime_ns

        # Second call must not rewrite the file (no mtime bump).
        profiles._write_bbl_machine_full_shims()
        self.assertEqual(os.stat(path).st_mtime_ns, first_mtime)


if __name__ == "__main__":
    unittest.main()
