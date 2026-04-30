# Defer flattening of user-imported profiles to slice time — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Store user-imported filament and process profiles in raw, GUI-shaped form (with `inherits` preserved); resolve the inheritance chain at slice and listing time. Stamp `filament_id` at import for AMS identity and reject collisions on overlapping `compatible_printers` sets.

**Architecture:** `app/profiles.py` becomes the locus of change: `resolve_profile_by_name` is made strict (raise on broken chain); `materialize_filament_import` / `materialize_process_import` are rewritten to keep `inherits` and apply minimal stamping; new helpers compute resolved `compatible_printers` and detect AMS-scope `filament_id` collisions. `app/main.py` preview endpoints return the merged form in a renamed `resolved_profile` field while the POST endpoints save the raw form. Existing flattened user files in `/data` keep working unchanged.

**Tech Stack:** Python 3, FastAPI, pytest (run in Docker via `.venv/bin/pytest`), Pydantic.

**Reference:** spec at `docs/superpowers/specs/2026-04-29-defer-user-profile-flattening-design.md`.

---

## File structure

**Modified:**
- `app/profiles.py` — strict resolver, listing-side wraps, new helpers, rewritten materializers.
- `app/main.py` — preview endpoints return merged form; POST endpoints save raw and derive response fields from resolved chain.
- `app/models.py` — rename `resolved_payload` → `resolved_profile` on both preview models, update field descriptions.
- `app/config.py` — bump `API_REVISION`.

**Modified tests:**
- `tests/test_import_endpoints.py` — every reference to `resolved_payload`, plus new tests for AMS-scope uniqueness and round-trip raw/resolved behavior.
- `tests/test_filament_vendor_resolution.py` — `test_materialize_is_deterministic_across_two_calls` keeps working (same raw output on both calls); may need a verification check that no merging happens.
- `tests/test_process_import.py` — parallel updates for process imports.

**New tests:**
- `tests/test_strict_resolution.py` — covers `resolve_profile_by_name` strict behavior and the listing-side wraps.

---

## How to run tests

A local Python venv has already been set up at `.venv/` with `requirements.txt` + `requirements-dev.txt` installed. Run tests directly:

```bash
.venv/bin/pytest tests/<file>::<class>::<test> -v
```

The tests are pure Python (FastAPI + pytest) and do not require the OrcaSlicer binary or vendor profile bundle — every test class builds its own temp profile fixture. Docker is **not** needed for the dev loop.

Baseline before Task 1: `130 passed, 1 skipped`.

---

## Task 1: Make `resolve_profile_by_name` strict

**Files:**
- Modify: `app/profiles.py:565-599`
- Test: `tests/test_strict_resolution.py` (new)

**Context:** Today, `resolve_profile_by_name` falls back to a partial profile when a parent reference can't be resolved (`app/profiles.py:588-594`). This task makes it raise `ProfileNotFoundError` instead. Listing-side callers that today rely on the silent fallback will be wrapped in the next two tasks.

- [ ] **Step 1: Create the new test file with the strict-resolution unit tests**

Write `tests/test_strict_resolution.py`:

```python
import shutil
import tempfile
import unittest
from pathlib import Path

from app import profiles


class StrictResolveProfileByNameTests(unittest.TestCase):
    """`resolve_profile_by_name` raises on broken inherits chains."""

    def setUp(self) -> None:
        self.tempdir = tempfile.mkdtemp(prefix="orcaslicer-cli-strict-")
        self.profiles_dir = Path(self.tempdir) / "profiles"
        self.user_dir = Path(self.tempdir) / "user"
        self.profiles_dir.mkdir(parents=True, exist_ok=True)
        self.user_dir.mkdir(parents=True, exist_ok=True)

        self._old_profiles_dir = profiles.PROFILES_DIR
        self._old_user_profiles_dir = profiles.USER_PROFILES_DIR
        profiles.PROFILES_DIR = str(self.profiles_dir)
        profiles.USER_PROFILES_DIR = str(self.user_dir)

    def tearDown(self) -> None:
        profiles.PROFILES_DIR = self._old_profiles_dir
        profiles.USER_PROFILES_DIR = self._old_user_profiles_dir
        profiles._raw_profiles.clear()
        profiles._type_map.clear()
        profiles._vendor_map.clear()
        profiles._name_index.clear()
        profiles._resolved_cache.clear()
        profiles._setting_id_index.clear()
        shutil.rmtree(self.tempdir)

    def _index_profile(self, vendor: str, data: dict, category: str) -> str:
        key = profiles._profile_key(vendor, str(data["name"]))
        profiles._index_profile(key, data, category, vendor)
        return key

    def test_raises_when_inherits_parent_is_unknown(self) -> None:
        self._index_profile(
            "User",
            {"name": "Child", "inherits": "Missing Parent"},
            "filament",
        )

        with self.assertRaises(profiles.ProfileNotFoundError) as ctx:
            profiles.resolve_profile_by_name("User::Child")

        msg = str(ctx.exception)
        self.assertIn("Child", msg)
        self.assertIn("Missing Parent", msg)

    def test_raises_when_intermediate_parent_in_chain_is_missing(self) -> None:
        self._index_profile(
            "User",
            {"name": "Leaf", "inherits": "Middle"},
            "filament",
        )
        self._index_profile(
            "User",
            {"name": "Middle", "inherits": "Root Gone"},
            "filament",
        )

        with self.assertRaises(profiles.ProfileNotFoundError) as ctx:
            profiles.resolve_profile_by_name("User::Leaf")

        msg = str(ctx.exception)
        self.assertIn("Root Gone", msg)

    def test_returns_merged_dict_on_successful_chain(self) -> None:
        self._index_profile(
            "User",
            {"name": "Parent", "filament_type": ["PLA"], "filament_id": "X1"},
            "filament",
        )
        self._index_profile(
            "User",
            {"name": "Child", "inherits": "Parent", "nozzle_temperature": ["230"]},
            "filament",
        )

        resolved = profiles.resolve_profile_by_name("User::Child")

        self.assertIsNotNone(resolved)
        self.assertEqual(resolved["filament_type"], ["PLA"])
        self.assertEqual(resolved["filament_id"], "X1")
        self.assertEqual(resolved["nozzle_temperature"], ["230"])
        self.assertEqual(resolved["name"], "Child")
```

- [ ] **Step 2: Run the tests to verify they fail**

```bash
.venv/bin/pytest tests/test_strict_resolution.py -v
```

Expected: the first two tests FAIL (today silently returns the partial child); the third test PASSES (today already merges correctly).

- [ ] **Step 3: Edit `app/profiles.py:565-599` to raise on broken chains**

Replace the body of `resolve_profile_by_name` so missing parents raise `ProfileNotFoundError`. Find the existing function and replace it with:

```python
def resolve_profile_by_name(name: str) -> dict[str, Any] | None:
    """Resolve a single profile's inheritance chain, with memoization.

    Raises `ProfileNotFoundError` if any link in the chain cannot be
    resolved. Returns `None` only when the requested name itself does
    not exist in the index (the caller can decide whether that is a
    hard error). Once a profile is found, every parent reference must
    resolve.
    """
    profile_key = name if name in _raw_profiles else _select_profile_key_by_name(name)
    if profile_key is None:
        return None

    if profile_key in _resolved_cache:
        return _resolved_cache[profile_key]

    profile = _raw_profiles.get(profile_key)
    if profile is None:
        return None

    parent_name = profile.get("inherits")
    if parent_name:
        parent_key = _resolve_parent_key(
            parent_name,
            category=_type_map.get(profile_key, ""),
            preferred_vendor=_vendor_map.get(profile_key, ""),
        )
        if parent_key is None:
            raise ProfileNotFoundError(
                f"Profile '{_display_name(profile_key)}' inherits from "
                f"'{parent_name}', which is not loaded."
            )
        parent = resolve_profile_by_name(parent_key)
        if parent is None:
            raise ProfileNotFoundError(
                f"Profile '{_display_name(profile_key)}' inherits from "
                f"'{parent_name}', which could not be resolved."
            )
        merged = dict(parent)
        merged.update(profile)
    else:
        merged = dict(profile)

    _resolved_cache[profile_key] = merged
    return merged
```

- [ ] **Step 4: Run the new tests to verify they pass**

```bash
.venv/bin/pytest tests/test_strict_resolution.py -v
```

Expected: all 3 tests PASS.

- [ ] **Step 5: Run the broader test suite to check for regressions**

```bash
.venv/bin/pytest tests/ -v
```

Expected: most existing tests still pass. Some listing/import tests may now fail because they rely on the silent fallback against intentionally-broken vendor fixtures. Note any failures — these will be addressed in Tasks 2 and 3 by wrapping the listing-side iterators.

- [ ] **Step 6: Commit**

```bash
git add app/profiles.py tests/test_strict_resolution.py
git commit -m "Make profile inheritance resolution strict

- raise ProfileNotFoundError when a parent reference does not resolve, instead of falling back to a partial child profile
- callers that need to tolerate broken chains in vendor data must catch the exception explicitly (handled in subsequent commits)"
```

---

## Task 2: Wrap `_iter_known_filament_names_and_ids` with try/except + log

**Files:**
- Modify: `app/profiles.py:187-207`
- Test: `tests/test_strict_resolution.py` (extend)

**Context:** This iterator computes "what filament_ids are already taken?" during a custom filament import (called from `_generate_custom_filament_id`). After Task 1, a single broken vendor profile in the index would raise here and 500 the user's import. We want the iteration to skip broken profiles and log a warning instead.

- [ ] **Step 1: Add a test verifying that import-side iteration tolerates a broken vendor profile**

Append to `tests/test_strict_resolution.py`:

```python
class ListingIterationTolerantWrapTests(unittest.TestCase):
    """Listing-side iteration in profiles.py skips broken chains with a log."""

    def setUp(self) -> None:
        self.tempdir = tempfile.mkdtemp(prefix="orcaslicer-cli-tolerant-")
        self.profiles_dir = Path(self.tempdir) / "profiles"
        self.user_dir = Path(self.tempdir) / "user"
        self.profiles_dir.mkdir(parents=True, exist_ok=True)
        self.user_dir.mkdir(parents=True, exist_ok=True)

        self._old_profiles_dir = profiles.PROFILES_DIR
        self._old_user_profiles_dir = profiles.USER_PROFILES_DIR
        profiles.PROFILES_DIR = str(self.profiles_dir)
        profiles.USER_PROFILES_DIR = str(self.user_dir)

    def tearDown(self) -> None:
        profiles.PROFILES_DIR = self._old_profiles_dir
        profiles.USER_PROFILES_DIR = self._old_user_profiles_dir
        profiles._raw_profiles.clear()
        profiles._type_map.clear()
        profiles._vendor_map.clear()
        profiles._name_index.clear()
        profiles._resolved_cache.clear()
        profiles._setting_id_index.clear()
        shutil.rmtree(self.tempdir)

    def _index(self, vendor: str, data: dict, category: str) -> str:
        key = profiles._profile_key(vendor, str(data["name"]))
        profiles._index_profile(key, data, category, vendor)
        return key

    def test_iter_known_filament_names_and_ids_skips_broken_chain(self) -> None:
        # A healthy filament with a direct id.
        self._index(
            "BBL",
            {
                "name": "Healthy",
                "filament_id": "GFA01",
                "filament_type": ["PLA"],
            },
            "filament",
        )
        # A broken filament with no direct id and an unresolvable parent.
        # _iter_known_filament_names_and_ids only walks the chain when the
        # raw profile lacks filament_id, so this exercises the fallback.
        self._index(
            "BBL",
            {"name": "Broken", "inherits": "Does Not Exist"},
            "filament",
        )

        with self.assertLogs(profiles.logger, level="WARNING") as cap:
            pairs = profiles._iter_known_filament_names_and_ids()

        ids = {fid for _, fid in pairs}
        self.assertIn("GFA01", ids)
        self.assertTrue(
            any("Broken" in record and "Does Not Exist" in record for record in cap.output),
            f"expected a warning naming the broken profile and parent, got {cap.output!r}",
        )
```

- [ ] **Step 2: Run the test to verify it fails**

```bash
.venv/bin/pytest tests/test_strict_resolution.py::ListingIterationTolerantWrapTests -v
```

Expected: FAIL with `ProfileNotFoundError` propagating out of `_iter_known_filament_names_and_ids`.

- [ ] **Step 3: Wrap the resolution call in `_iter_known_filament_names_and_ids`**

Open `app/profiles.py` at line 187. Replace the body of the function with:

```python
def _iter_known_filament_names_and_ids() -> list[tuple[str, str]]:
    """Return known (logical_filament_name, filament_id) pairs from loaded profiles.

    Profiles with broken inherits chains are skipped with a warning, so a
    single bad vendor JSON cannot block id-collision detection during a
    user import.
    """
    pairs: list[tuple[str, str]] = []
    for profile_key, raw in _raw_profiles.items():
        if _type_map.get(profile_key) != "filament":
            continue

        profile_name = str(raw.get("name", _display_name(profile_key)))
        logical_name = _logical_filament_name(profile_name)

        # Prefer the raw profile's id; fallback to the resolved chain id.
        filament_id = _extract_filament_id(raw)
        if not filament_id:
            try:
                resolved = resolve_profile_by_name(profile_key)
            except ProfileNotFoundError as exc:
                logger.warning(
                    "Skipping filament '%s' during id-collision iteration: %s",
                    profile_key, exc,
                )
                continue
            if resolved:
                filament_id = _extract_filament_id(resolved)

        if not filament_id or filament_id == "null":
            continue
        pairs.append((logical_name, filament_id))
    return pairs
```

- [ ] **Step 4: Run the new test to verify it passes**

```bash
.venv/bin/pytest tests/test_strict_resolution.py::ListingIterationTolerantWrapTests::test_iter_known_filament_names_and_ids_skips_broken_chain -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/profiles.py tests/test_strict_resolution.py
git commit -m "Skip broken filament chains in id-collision iteration

- _iter_known_filament_names_and_ids catches ProfileNotFoundError when the resolution fallback is needed, logs a warning, and continues
- prevents a single broken vendor profile from blocking unrelated user imports under the new strict resolver"
```

---

## Task 3: Wrap `_is_ams_assignable_filament` listing-side use with try/except + log

**Files:**
- Modify: `app/profiles.py` — find the function `get_filament_profiles` (the one returning the list endpoint payload). Identify where `resolve_profile_by_name` is called for listing.
- Test: `tests/test_strict_resolution.py` (extend)

**Context:** `_is_ams_assignable_filament` itself is a pure predicate over a raw + resolved pair (`app/profiles.py:167-184`); it does not call `resolve_profile_by_name`. Its caller — the listing path — is what calls the resolver. We need the listing path to skip profiles whose chain is broken.

- [ ] **Step 1: Locate the listing-side resolution call site**

Run:

```bash
grep -n "resolve_profile_by_name" app/profiles.py
```

Expected output includes references inside `get_filament_profiles` (or its helpers) and inside `_iter_known_filament_names_and_ids` (already handled in Task 2). Read the full body of `get_filament_profiles` to identify exactly which `resolve_profile_by_name` call needs the wrap.

- [ ] **Step 2: Add a test verifying that the filament listing skips broken profiles**

Append to `tests/test_strict_resolution.py`:

```python
    def test_get_filament_profiles_skips_broken_chain_with_warning(self) -> None:
        # Healthy filament that resolves cleanly.
        self._index(
            "BBL",
            {
                "name": "Healthy",
                "setting_id": "GFA01",
                "instantiation": "true",
                "filament_id": "GFA01",
                "filament_type": ["PLA"],
                "compatible_printers": ["Bambu Lab A1 mini 0.4 nozzle"],
            },
            "filament",
        )
        # Broken filament inheriting from a non-existent parent.
        self._index(
            "BBL",
            {
                "name": "BrokenLeaf",
                "setting_id": "GFA02",
                "instantiation": "true",
                "inherits": "Does Not Exist",
                "compatible_printers": ["Bambu Lab A1 mini 0.4 nozzle"],
            },
            "filament",
        )
        # Machine entry referenced by compatible_printers (so the listing
        # code's machine filter can be exercised without it).
        self._index(
            "BBL",
            {
                "name": "Bambu Lab A1 mini 0.4 nozzle",
                "setting_id": "GM020",
                "instantiation": "true",
                "printer_model": "Bambu Lab A1 mini",
                "nozzle_diameter": ["0.4"],
            },
            "machine",
        )

        with self.assertLogs(profiles.logger, level="WARNING") as cap:
            listed = profiles.get_filament_profiles()

        names = {p["name"] for p in listed}
        self.assertIn("Healthy", names)
        self.assertNotIn("BrokenLeaf", names)
        self.assertTrue(
            any("BrokenLeaf" in record for record in cap.output),
            f"expected a warning naming the broken profile, got {cap.output!r}",
        )
```

- [ ] **Step 3: Run the test to verify it fails**

```bash
.venv/bin/pytest tests/test_strict_resolution.py::ListingIterationTolerantWrapTests::test_get_filament_profiles_skips_broken_chain_with_warning -v
```

Expected: FAIL with `ProfileNotFoundError` propagating out of the listing code.

- [ ] **Step 4: Wrap the listing-side resolver call**

In the listing code identified in Step 1, find the call to `resolve_profile_by_name` and wrap it with `try/except ProfileNotFoundError`. The shape:

```python
try:
    resolved = resolve_profile_by_name(profile_key)
except ProfileNotFoundError as exc:
    logger.warning(
        "Skipping filament '%s' from listing: %s",
        profile_key, exc,
    )
    continue
if resolved is None:
    continue
```

The exact insertion site depends on the function structure as it exists in the file. Read the function before editing; place the wrap at the existing `resolve_profile_by_name` call in the iteration over filament profile keys.

If the listing code calls a helper (e.g. `_is_ams_assignable_filament(raw, resolved, ...)`) that depends on `resolved`, the `continue` after the warning is the correct skip behavior.

- [ ] **Step 5: Run the new test to verify it passes**

```bash
.venv/bin/pytest tests/test_strict_resolution.py::ListingIterationTolerantWrapTests::test_get_filament_profiles_skips_broken_chain_with_warning -v
```

Expected: PASS.

- [ ] **Step 6: Run the full test suite**

```bash
.venv/bin/pytest tests/ -v
```

Expected: tests that previously failed in Task 1's broader run because they rely on silent-fallback in listings should now pass. Note any remaining failures — those should be ones we expect to update in Tasks 7-9.

- [ ] **Step 7: Commit**

```bash
git add app/profiles.py tests/test_strict_resolution.py
git commit -m "Skip broken filament chains in /profiles/filaments listing

- catch ProfileNotFoundError from the strict resolver in the listing path, log a warning, and exclude the unresolvable profile
- broken vendor profiles no longer take down the entire picker"
```

---

## Task 4: Add `_resolve_chain_for_payload` helper

**Files:**
- Modify: `app/profiles.py` — add a new private helper near `materialize_filament_import`.
- Test: `tests/test_strict_resolution.py` (extend) or `tests/test_resolve_chain_for_payload.py` (new) — your call; new file is cleaner.

**Context:** Both the import-time materializer (for AMS uniqueness check) and the preview endpoint (for `resolved_profile`) need to walk the chain of an as-yet-unindexed payload. This helper does that without registering the payload.

- [ ] **Step 1: Create the test file**

Write `tests/test_resolve_chain_for_payload.py`:

```python
import shutil
import tempfile
import unittest
from pathlib import Path

from app import profiles


class ResolveChainForPayloadTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.mkdtemp(prefix="orcaslicer-cli-chainpayload-")
        self.profiles_dir = Path(self.tempdir) / "profiles"
        self.user_dir = Path(self.tempdir) / "user"
        self.profiles_dir.mkdir(parents=True, exist_ok=True)
        self.user_dir.mkdir(parents=True, exist_ok=True)

        self._old_profiles_dir = profiles.PROFILES_DIR
        self._old_user_profiles_dir = profiles.USER_PROFILES_DIR
        profiles.PROFILES_DIR = str(self.profiles_dir)
        profiles.USER_PROFILES_DIR = str(self.user_dir)

        # A vendor parent that the payload will inherit from.
        profiles._index_profile(
            "BBL::Bambu PLA Basic @BBL A1M",
            {
                "name": "Bambu PLA Basic @BBL A1M",
                "setting_id": "GFA00_A1M",
                "instantiation": "true",
                "filament_id": "GFA00",
                "filament_type": ["PLA"],
                "compatible_printers": ["Bambu Lab A1 mini 0.4 nozzle"],
                "nozzle_temperature": ["220"],
            },
            "filament",
            "BBL",
        )

    def tearDown(self) -> None:
        profiles.PROFILES_DIR = self._old_profiles_dir
        profiles.USER_PROFILES_DIR = self._old_user_profiles_dir
        profiles._raw_profiles.clear()
        profiles._type_map.clear()
        profiles._vendor_map.clear()
        profiles._name_index.clear()
        profiles._resolved_cache.clear()
        profiles._setting_id_index.clear()
        shutil.rmtree(self.tempdir)

    def test_returns_payload_when_no_inherits(self) -> None:
        payload = {"name": "Standalone", "filament_type": ["PLA"], "filament_id": "X1"}

        merged = profiles._resolve_chain_for_payload(payload, category="filament")

        self.assertEqual(merged, payload)
        # Original input is not mutated.
        self.assertNotIn("from", payload)

    def test_overlays_payload_on_resolved_parent(self) -> None:
        payload = {
            "name": "My Custom",
            "inherits": "Bambu PLA Basic @BBL A1M",
            "nozzle_temperature": ["230"],
        }

        merged = profiles._resolve_chain_for_payload(payload, category="filament")

        # Inherited from parent.
        self.assertEqual(merged["filament_type"], ["PLA"])
        self.assertEqual(merged["filament_id"], "GFA00")
        self.assertEqual(
            merged["compatible_printers"], ["Bambu Lab A1 mini 0.4 nozzle"]
        )
        # Overlaid by payload.
        self.assertEqual(merged["nozzle_temperature"], ["230"])
        self.assertEqual(merged["name"], "My Custom")

    def test_raises_when_parent_is_unknown(self) -> None:
        payload = {"name": "Orphan", "inherits": "No Such Parent"}

        with self.assertRaises(profiles.ProfileNotFoundError) as ctx:
            profiles._resolve_chain_for_payload(payload, category="filament")

        self.assertIn("No Such Parent", str(ctx.exception))
```

- [ ] **Step 2: Run the tests to verify they fail**

```bash
.venv/bin/pytest tests/test_resolve_chain_for_payload.py -v
```

Expected: FAIL with `AttributeError: module 'app.profiles' has no attribute '_resolve_chain_for_payload'`.

- [ ] **Step 3: Add the helper to `app/profiles.py`**

Place this function near `_resolve_filament_parent_ref` and `_resolve_process_parent_ref` (around line 233-318):

```python
def _resolve_chain_for_payload(
    payload: dict[str, Any],
    *,
    category: str,
) -> dict[str, Any]:
    """Resolve a payload's inherits chain in memory without indexing it.

    Returns the fully merged dict (parent values overlaid with the
    payload's own keys). Raises `ProfileNotFoundError` if `inherits` is
    set but the parent or any link in its chain cannot be resolved.

    `category` must be `"filament"` or `"process"` (machine imports do
    not exist).
    """
    inherits_raw = payload.get("inherits")
    if not isinstance(inherits_raw, str) or not inherits_raw.strip():
        return dict(payload)

    inherits = inherits_raw.strip()
    if category == "filament":
        parent_key = _resolve_filament_parent_ref(inherits)
    elif category == "process":
        parent_key = _resolve_process_parent_ref(inherits)
    else:
        raise ValueError(f"Unsupported category for chain resolution: {category!r}")

    if parent_key is None:
        raise ProfileNotFoundError(
            f"Parent {category} profile '{inherits}' not found"
        )

    parent_resolved = resolve_profile_by_name(parent_key)
    if parent_resolved is None:
        raise ProfileNotFoundError(
            f"Failed to resolve parent {category} profile '{inherits}'"
        )

    merged = dict(parent_resolved)
    merged.update(payload)
    return merged
```

- [ ] **Step 4: Run the tests to verify they pass**

```bash
.venv/bin/pytest tests/test_resolve_chain_for_payload.py -v
```

Expected: all 3 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add app/profiles.py tests/test_resolve_chain_for_payload.py
git commit -m "Add _resolve_chain_for_payload helper

- walks the inherits chain of an unindexed payload by looking up the parent in the profile index, then overlays the payload's own keys on the resolved parent
- used in the next steps to compute resolved compatible_printers for AMS-uniqueness checks and to populate the resolved_profile field on the preview endpoints"
```

---

## Task 5: Add `_compatible_printers_set_for_payload` helper

**Files:**
- Modify: `app/profiles.py` — add a new helper.
- Test: `tests/test_resolve_chain_for_payload.py` (extend).

**Context:** The AMS-scope uniqueness check needs the resolved `compatible_printers` set for the new import. We already have `_resolve_chain_for_payload` from Task 4; this is a thin wrapper that extracts the set.

- [ ] **Step 1: Add the test cases**

Append to `tests/test_resolve_chain_for_payload.py`:

```python
class CompatiblePrintersSetForPayloadTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.mkdtemp(prefix="orcaslicer-cli-cpset-")
        self.profiles_dir = Path(self.tempdir) / "profiles"
        self.user_dir = Path(self.tempdir) / "user"
        self.profiles_dir.mkdir(parents=True, exist_ok=True)
        self.user_dir.mkdir(parents=True, exist_ok=True)

        self._old_profiles_dir = profiles.PROFILES_DIR
        self._old_user_profiles_dir = profiles.USER_PROFILES_DIR
        profiles.PROFILES_DIR = str(self.profiles_dir)
        profiles.USER_PROFILES_DIR = str(self.user_dir)

        profiles._index_profile(
            "BBL::Parent A1M",
            {
                "name": "Parent A1M",
                "setting_id": "GFA00_A1M",
                "instantiation": "true",
                "filament_id": "GFA00",
                "filament_type": ["PLA"],
                "compatible_printers": ["Bambu Lab A1 mini 0.4 nozzle"],
            },
            "filament",
            "BBL",
        )

    def tearDown(self) -> None:
        profiles.PROFILES_DIR = self._old_profiles_dir
        profiles.USER_PROFILES_DIR = self._old_user_profiles_dir
        profiles._raw_profiles.clear()
        profiles._type_map.clear()
        profiles._vendor_map.clear()
        profiles._name_index.clear()
        profiles._resolved_cache.clear()
        profiles._setting_id_index.clear()
        shutil.rmtree(self.tempdir)

    def test_reads_directly_from_payload_when_present(self) -> None:
        payload = {
            "name": "Self",
            "compatible_printers": ["X1C 0.4", "X1C 0.6"],
        }

        result = profiles._compatible_printers_set_for_payload(
            payload, category="filament"
        )

        self.assertEqual(result, {"X1C 0.4", "X1C 0.6"})

    def test_inherits_from_parent_when_absent_on_payload(self) -> None:
        payload = {
            "name": "Child",
            "inherits": "Parent A1M",
        }

        result = profiles._compatible_printers_set_for_payload(
            payload, category="filament"
        )

        self.assertEqual(result, {"Bambu Lab A1 mini 0.4 nozzle"})

    def test_returns_empty_set_when_chain_provides_none(self) -> None:
        payload = {"name": "Empty"}

        result = profiles._compatible_printers_set_for_payload(
            payload, category="filament"
        )

        self.assertEqual(result, set())
```

- [ ] **Step 2: Run the tests to verify they fail**

```bash
.venv/bin/pytest tests/test_resolve_chain_for_payload.py::CompatiblePrintersSetForPayloadTests -v
```

Expected: FAIL with `AttributeError`.

- [ ] **Step 3: Add the helper to `app/profiles.py`**

Place it directly below `_resolve_chain_for_payload`:

```python
def _compatible_printers_set_for_payload(
    payload: dict[str, Any],
    *,
    category: str,
) -> set[str]:
    """Return the resolved `compatible_printers` set for a payload.

    Walks the inherits chain (via `_resolve_chain_for_payload`) so that
    a payload that does not declare `compatible_printers` inherits it
    from the parent. Returns an empty set if no value is found anywhere
    in the chain.
    """
    merged = _resolve_chain_for_payload(payload, category=category)
    raw_value = merged.get("compatible_printers")
    if not isinstance(raw_value, list):
        return set()
    return {str(item) for item in raw_value if isinstance(item, str)}
```

- [ ] **Step 4: Run the tests to verify they pass**

```bash
.venv/bin/pytest tests/test_resolve_chain_for_payload.py::CompatiblePrintersSetForPayloadTests -v
```

Expected: all 3 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add app/profiles.py tests/test_resolve_chain_for_payload.py
git commit -m "Add _compatible_printers_set_for_payload helper

- extracts the resolved compatible_printers set for a payload, walking the inherits chain so thin imports inherit the parent's printer scope
- consumed by the AMS-uniqueness check added in the next commit"
```

---

## Task 6: Add `_check_filament_id_ams_scope` helper

**Files:**
- Modify: `app/profiles.py` — add the conflict detection helper.
- Test: `tests/test_resolve_chain_for_payload.py` (extend).

**Context:** Given a candidate `(filament_id, compatible_printers)` and an optional `exclude_setting_id` (for the replace case), this function scans the index and raises if any other filament profile shares the id with an overlapping printer set. It also fails closed when either side has an empty resolved set.

- [ ] **Step 1: Add the test cases**

Append to `tests/test_resolve_chain_for_payload.py`:

```python
class CheckFilamentIdAmsScopeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.mkdtemp(prefix="orcaslicer-cli-amscope-")
        self.profiles_dir = Path(self.tempdir) / "profiles"
        self.user_dir = Path(self.tempdir) / "user"
        self.profiles_dir.mkdir(parents=True, exist_ok=True)
        self.user_dir.mkdir(parents=True, exist_ok=True)

        self._old_profiles_dir = profiles.PROFILES_DIR
        self._old_user_profiles_dir = profiles.USER_PROFILES_DIR
        profiles.PROFILES_DIR = str(self.profiles_dir)
        profiles.USER_PROFILES_DIR = str(self.user_dir)

        profiles._index_profile(
            "BBL::Bambu PLA Basic @BBL A1M",
            {
                "name": "Bambu PLA Basic @BBL A1M",
                "setting_id": "GFA00_A1M",
                "instantiation": "true",
                "filament_id": "GFA00",
                "filament_type": ["PLA"],
                "compatible_printers": ["Bambu Lab A1 mini 0.4 nozzle"],
            },
            "filament",
            "BBL",
        )
        profiles._index_profile(
            "BBL::Bambu PLA Basic @BBL X1C",
            {
                "name": "Bambu PLA Basic @BBL X1C",
                "setting_id": "GFA00_X1C",
                "instantiation": "true",
                "filament_id": "GFA00",
                "filament_type": ["PLA"],
                "compatible_printers": ["Bambu Lab X1 Carbon 0.4 nozzle"],
            },
            "filament",
            "BBL",
        )

    def tearDown(self) -> None:
        profiles.PROFILES_DIR = self._old_profiles_dir
        profiles.USER_PROFILES_DIR = self._old_user_profiles_dir
        profiles._raw_profiles.clear()
        profiles._type_map.clear()
        profiles._vendor_map.clear()
        profiles._name_index.clear()
        profiles._resolved_cache.clear()
        profiles._setting_id_index.clear()
        shutil.rmtree(self.tempdir)

    def test_disjoint_compatible_printers_allow_shared_id(self) -> None:
        # No exception expected.
        profiles._check_filament_id_ams_scope(
            filament_id="GFA00",
            compatible_printers={"Bambu Lab P1S 0.4 nozzle"},
            exclude_setting_id=None,
        )

    def test_overlapping_compatible_printers_raise_value_error(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            profiles._check_filament_id_ams_scope(
                filament_id="GFA00",
                compatible_printers={"Bambu Lab A1 mini 0.4 nozzle"},
                exclude_setting_id=None,
            )
        msg = str(ctx.exception)
        self.assertIn("GFA00", msg)
        self.assertIn("Bambu Lab A1 mini 0.4 nozzle", msg)
        self.assertIn("Bambu PLA Basic @BBL A1M", msg)

    def test_empty_compatible_printers_fails_closed_against_any_match(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            profiles._check_filament_id_ams_scope(
                filament_id="GFA00",
                compatible_printers=set(),
                exclude_setting_id=None,
            )
        self.assertIn("GFA00", str(ctx.exception))

    def test_excluded_setting_id_is_ignored(self) -> None:
        # Replace flow: the existing A1M profile is being overwritten by
        # itself; passing its setting_id excludes it from the check.
        profiles._check_filament_id_ams_scope(
            filament_id="GFA00",
            compatible_printers={"Bambu Lab A1 mini 0.4 nozzle"},
            exclude_setting_id="GFA00_A1M",
        )

    def test_no_match_for_filament_id_is_a_noop(self) -> None:
        profiles._check_filament_id_ams_scope(
            filament_id="GFNOTUSED",
            compatible_printers={"Bambu Lab A1 mini 0.4 nozzle"},
            exclude_setting_id=None,
        )
```

- [ ] **Step 2: Run the tests to verify they fail**

```bash
.venv/bin/pytest tests/test_resolve_chain_for_payload.py::CheckFilamentIdAmsScopeTests -v
```

Expected: FAIL with `AttributeError`.

- [ ] **Step 3: Add the helper to `app/profiles.py`**

Place it below `_compatible_printers_set_for_payload`:

```python
def _check_filament_id_ams_scope(
    *,
    filament_id: str,
    compatible_printers: set[str],
    exclude_setting_id: str | None,
) -> None:
    """Validate that `filament_id` does not collide on overlapping AMS scope.

    Two filament profiles may share `filament_id` iff their resolved
    `compatible_printers` sets are disjoint (different printers ⇒
    different AMS scopes). An empty set is treated as "all printers"
    and fails closed.

    `exclude_setting_id`, when provided, is the setting_id of the user
    profile being replaced — it is excluded from the comparison so a
    profile can be re-imported under itself.

    Raises `ValueError` (with a user-facing message) on conflict.
    """
    if not filament_id or filament_id == "null":
        return

    for other_key, raw in _raw_profiles.items():
        if _type_map.get(other_key) != "filament":
            continue
        if _extract_filament_id(raw) != filament_id:
            # Only check raw filament_id for performance; profiles that
            # inherit filament_id will be caught when the parent itself
            # is iterated (and at slice time inheriting the parent's id
            # is exactly what we are guarding against by stamping our
            # own id at import).
            continue
        other_setting_id = str(raw.get("setting_id", "")).strip()
        if exclude_setting_id and other_setting_id == exclude_setting_id:
            continue

        other_payload = dict(raw)
        try:
            other_printers = _compatible_printers_set_for_payload(
                other_payload, category="filament"
            )
        except ProfileNotFoundError:
            # Broken chain on the existing profile — skip it for the
            # collision check (we cannot conclude a real conflict).
            logger.warning(
                "Skipping AMS-scope check against '%s' (filament_id=%s): "
                "broken inherits chain",
                other_key, filament_id,
            )
            continue

        # Empty on either side ⇒ "all printers" ⇒ assume conflict.
        if not compatible_printers or not other_printers:
            overlap_desc = "<all printers>"
        else:
            overlap = compatible_printers & other_printers
            if not overlap:
                continue
            overlap_desc = ", ".join(sorted(overlap))

        existing_name = str(raw.get("name", _display_name(other_key)))
        raise ValueError(
            f"filament_id '{filament_id}' is already used by profile "
            f"'{existing_name}' on overlapping printers: {overlap_desc}."
        )
```

- [ ] **Step 4: Run the tests to verify they pass**

```bash
.venv/bin/pytest tests/test_resolve_chain_for_payload.py::CheckFilamentIdAmsScopeTests -v
```

Expected: all 5 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add app/profiles.py tests/test_resolve_chain_for_payload.py
git commit -m "Add AMS-scope filament_id uniqueness check

- a filament_id may be shared between profiles only when their resolved compatible_printers sets are disjoint; an empty set fails closed
- the replace flow excludes the profile being overwritten from the comparison via exclude_setting_id"
```

---

## Task 7: Rewrite `materialize_filament_import` to keep `inherits`

**Files:**
- Modify: `app/profiles.py:244-304` (the existing `materialize_filament_import`).
- Test: `tests/test_filament_vendor_resolution.py:53-60` (`test_materialize_is_deterministic_across_two_calls` — keeps working but should be supplemented), plus new test cases.

**Context:** The function becomes a light stamper instead of a flattener. It validates the parent's existence, synthesizes `setting_id` and `instantiation`, stamps `filament_id` (preserving direct caller value or generating one), runs the AMS-scope uniqueness check via the helper from Task 6, and returns the input dict (with stamps) **without merging the parent**.

- [ ] **Step 1: Open `tests/test_filament_vendor_resolution.py` and add the new test class for raw-form materialization**

The existing class is `FilamentVendorResolutionTests` (defined at line 10). Subclass it so the new tests reuse its `setUp` / `tearDown` / `_write_fixture` (which sets up SUNLU PLA+ @BBL A1M, SUNLU PLA+ @base, etc.). Append at the end of the file (keeping the existing `test_materialize_is_deterministic_across_two_calls` intact):

```python
class MaterializeFilamentImportRawFormTests(FilamentVendorResolutionTests):
    """The new raw-form behavior of materialize_filament_import."""

    def test_preserves_inherits_and_does_not_merge_parent(self) -> None:
        result = profiles.materialize_filament_import({
            "name": "My SUNLU copy",
            "inherits": "SUNLU PLA+ @BBL A1M",
        })

        self.assertEqual(result["name"], "My SUNLU copy")
        self.assertEqual(result["inherits"], "SUNLU PLA+ @BBL A1M")
        # Parent values must NOT be merged into the result.
        self.assertNotIn("filament_type", result)

    def test_synthesizes_setting_id_from_name_when_missing(self) -> None:
        result = profiles.materialize_filament_import({
            "name": "My SUNLU copy",
            "inherits": "SUNLU PLA+ @BBL A1M",
        })

        self.assertEqual(result["setting_id"], "My SUNLU copy")

    def test_keeps_directly_supplied_setting_id(self) -> None:
        result = profiles.materialize_filament_import({
            "name": "My SUNLU copy",
            "setting_id": "MYSUNLU",
            "inherits": "SUNLU PLA+ @BBL A1M",
        })

        self.assertEqual(result["setting_id"], "MYSUNLU")

    def test_stamps_instantiation_true_when_missing(self) -> None:
        result = profiles.materialize_filament_import({
            "name": "My SUNLU copy",
            "inherits": "SUNLU PLA+ @BBL A1M",
        })

        self.assertEqual(result["instantiation"], "true")

    def test_keeps_directly_supplied_filament_id_when_disjoint(self) -> None:
        # The parent SUNLU PLA+ @BBL A1M is compatible with A1 mini.
        # We claim a disjoint printer set so the uniqueness check passes.
        result = profiles.materialize_filament_import({
            "name": "My SUNLU copy",
            "inherits": "SUNLU PLA+ @BBL A1M",
            "filament_id": "PCUSTOM1",
            "compatible_printers": ["My Custom Printer"],
        })

        self.assertEqual(result["filament_id"], "PCUSTOM1")

    def test_generates_filament_id_when_caller_does_not_supply_one(self) -> None:
        result = profiles.materialize_filament_import({
            "name": "My SUNLU copy",
            "inherits": "SUNLU PLA+ @BBL A1M",
        })

        fid = result["filament_id"]
        self.assertTrue(fid.startswith("P"))
        self.assertEqual(len(fid), 8)

    def test_raises_when_inherits_parent_is_unknown(self) -> None:
        with self.assertRaises(profiles.ProfileNotFoundError):
            profiles.materialize_filament_import({
                "name": "My orphan",
                "inherits": "Does Not Exist",
            })

    def test_rejects_directly_supplied_filament_id_on_overlapping_printers(self) -> None:
        # GFSNL03 is the parent's filament_id (SUNLU PLA+ @base, used by
        # SUNLU PLA+ @BBL A1M which is compatible with A1 mini). Pasting
        # it into a new import that also targets A1 mini collides.
        with self.assertRaises(ValueError) as ctx:
            profiles.materialize_filament_import({
                "name": "Pretender",
                "inherits": "SUNLU PLA+ @BBL A1M",
                "filament_id": "GFSNL03",
            })

        msg = str(ctx.exception)
        self.assertIn("GFSNL03", msg)

    def test_allows_directly_supplied_filament_id_on_disjoint_printers(self) -> None:
        # Same id as the existing vendor profile, but our resolved
        # compatible_printers do not overlap.
        result = profiles.materialize_filament_import({
            "name": "Disjoint Sibling",
            "inherits": "SUNLU PLA+ @BBL A1M",
            "filament_id": "GFSNL03",
            "compatible_printers": ["My Custom Printer"],
        })
        self.assertEqual(result["filament_id"], "GFSNL03")

    def test_rejects_when_resolved_compatible_printers_is_empty(self) -> None:
        # Build a chain whose resolved compatible_printers ends up empty.
        # SUNLU PLA+ @base inherits fdm_filament_pla and never sets
        # compatible_printers — so a payload inheriting from @base only
        # has an empty resolved set.
        with self.assertRaises(ValueError) as ctx:
            profiles.materialize_filament_import({
                "name": "Unscoped",
                "inherits": "SUNLU PLA+ @base",
                "filament_id": "GFSNL03",
            })

        self.assertIn("GFSNL03", str(ctx.exception))


# Helper base class for the new tests. If `VendorResolutionTestBase`
# does not exist in the file, refactor the existing setUp/tearDown into
# a base class first; or duplicate the setUp/tearDown into the new
# class verbatim.
```

Subclassing `FilamentVendorResolutionTests` (rather than copying setUp / tearDown) means the new tests share the existing fixture and any future setup change applies to both. The existing tests in `FilamentVendorResolutionTests` will run a second time as inherited tests in the subclass; that's harmless but if you'd rather avoid double-runs, override the inherited test methods with `pass` or extract the setup into a separate base class.

- [ ] **Step 2: Run the new tests to verify they fail**

```bash
.venv/bin/pytest tests/test_filament_vendor_resolution.py::MaterializeFilamentImportRawFormTests -v
```

Expected: most fail. Specifically:
- `test_preserves_inherits_and_does_not_merge_parent` — FAIL because today the function strips `inherits` and merges parent.
- `test_synthesizes_setting_id_from_name_when_missing` — likely PASS today (already done).
- `test_stamps_instantiation_true_when_missing` — PASS today.
- `test_keeps_directly_supplied_filament_id_when_disjoint` — PASS today.
- `test_generates_filament_id_when_caller_does_not_supply_one` — PASS today.
- `test_raises_when_inherits_parent_is_unknown` — PASS today.
- `test_rejects_directly_supplied_filament_id_on_overlapping_printers` — FAIL today (no uniqueness check on direct input).
- `test_allows_directly_supplied_filament_id_on_disjoint_printers` — PASS today (no check at all).
- `test_rejects_when_resolved_compatible_printers_is_empty` — FAIL today.

- [ ] **Step 3: Rewrite `materialize_filament_import`**

Replace the body of `materialize_filament_import` (`app/profiles.py:244-304`) with:

```python
def materialize_filament_import(data: dict[str, Any]) -> dict[str, Any]:
    """Lightly stamp an imported filament payload and return its raw form.

    The returned dict preserves `inherits` so the inheritance chain is
    resolved at slice / listing time. Stamping is limited to the fields
    needed for indexing and AMS identity:

    - `setting_id`: synthesized from `name` if missing.
    - `instantiation`: set to `"true"` if missing.
    - `filament_id`: kept verbatim if directly supplied; otherwise
      generated via `_generate_custom_filament_id`. Stamping the id at
      import time prevents it from inheriting from the parent (which
      would collide AMS identity for every clone of the parent).

    Validates that `inherits`, when set, points to a known filament
    profile, and that the resulting `filament_id` does not collide
    with another profile on overlapping `compatible_printers`.
    """
    name = data.get("name")
    if not isinstance(name, str) or not name.strip():
        raise ValueError("Missing or invalid 'name' field.")
    name = name.strip()

    setting_id = data.get("setting_id", name)
    if not isinstance(setting_id, str) or not setting_id.strip():
        raise ValueError("Missing or invalid 'setting_id' field.")
    setting_id = setting_id.strip()

    inherits = data.get("inherits")
    if isinstance(inherits, str) and inherits.strip():
        parent_name = _resolve_filament_parent_ref(inherits.strip())
        if not parent_name:
            raise ProfileNotFoundError(
                f"Filament parent '{inherits.strip()}' not found"
            )

    result = dict(data)
    result["name"] = name
    result["setting_id"] = setting_id
    if "instantiation" not in result:
        result["instantiation"] = "true"

    # Stamp filament_id (AMS identity).
    if _has_direct_filament_id(data):
        # Caller's value wins; keep it verbatim.
        filament_id = _extract_filament_id(data)
        result["filament_id"] = filament_id
    else:
        logical_name = _logical_filament_name(name)
        filament_id = _generate_custom_filament_id(logical_name)
        result["filament_id"] = filament_id

    # Validate AMS-scope uniqueness against currently loaded profiles.
    compat = _compatible_printers_set_for_payload(result, category="filament")
    _check_filament_id_ams_scope(
        filament_id=filament_id,
        compatible_printers=compat,
        exclude_setting_id=setting_id,
    )

    return result
```

Key behavior choices reflected here:
- `inherits`, `from`, `base_id`, and `filament_settings_id` are preserved as-is.
- The merged form is **not** computed inside materialize — `result` is the raw input plus stamps.
- `exclude_setting_id=setting_id` covers the replace flow: re-importing yourself does not collide with yourself.

- [ ] **Step 4: Run the new tests to verify they pass**

```bash
.venv/bin/pytest tests/test_filament_vendor_resolution.py::MaterializeFilamentImportRawFormTests -v
```

Expected: all 10 tests PASS.

- [ ] **Step 5: Run the existing deterministic test to verify it still passes**

```bash
.venv/bin/pytest tests/test_filament_vendor_resolution.py::FilamentVendorResolutionTests::test_materialize_is_deterministic_across_two_calls -v
```

Replace `FilamentVendorResolutionTests` with the actual test class name in the file (read the file to confirm). Expected: PASS.

- [ ] **Step 6: Run the full test_filament_vendor_resolution.py module**

```bash
.venv/bin/pytest tests/test_filament_vendor_resolution.py -v
```

Expected: all tests pass.

- [ ] **Step 7: Commit**

```bash
git add app/profiles.py tests/test_filament_vendor_resolution.py
git commit -m "Rewrite materialize_filament_import to keep inherits

- import payload is stamped (setting_id, instantiation, filament_id) but no longer merged with the parent; the saved file preserves inherits
- AMS identity is locked at import via filament_id stamping, with a uniqueness check against any other profile sharing the id on overlapping compatible_printers"
```

---

## Task 8: Rewrite `materialize_process_import` to keep `inherits`

**Files:**
- Modify: `app/profiles.py:321-366`.
- Test: `tests/test_process_import.py` (extend) and existing tests in this file may need updating.

**Context:** Parallel to Task 7 but simpler — process profiles have no `filament_id` / AMS concept.

- [ ] **Step 1: Read `tests/test_process_import.py` to identify tests that depend on flattening behavior**

```bash
.venv/bin/pytest tests/test_process_import.py -v
```

Note any tests asserting that the output has merged keys from the parent (e.g. expecting a key inherited from `0.20mm Standard @BBL A1M` to appear in materialize_process_import's result) — these will need updating.

Open `tests/test_process_import.py` and read it end-to-end.

- [ ] **Step 2: Add new test class for raw-form behavior**

The existing class `ProcessImportHappyPathTests` (line 13) defines setUp / tearDown that load the BBL fixture with `0.20mm Standard @BBL A1M`. Subclass it for the new tests (mirroring how `ProcessImportErrorPathTests` already does at line 109). Append to `tests/test_process_import.py`:

```python
class MaterializeProcessImportRawFormTests(ProcessImportHappyPathTests):
    """The new raw-form behavior of materialize_process_import."""

    def test_preserves_inherits_and_does_not_merge_parent(self) -> None:
        result = profiles.materialize_process_import({
            "name": "Custom Process",
            "inherits": "0.20mm Standard @BBL A1M",
            "outer_wall_speed": ["150"],
        })

        self.assertEqual(result["inherits"], "0.20mm Standard @BBL A1M")
        self.assertEqual(result["outer_wall_speed"], ["150"])
        # Parent's `inner_wall_speed` MUST NOT be merged into the result.
        self.assertNotIn("inner_wall_speed", result)

    def test_synthesizes_setting_id_from_name_when_missing(self) -> None:
        result = profiles.materialize_process_import({
            "name": "Custom Process",
            "inherits": "0.20mm Standard @BBL A1M",
        })

        self.assertEqual(result["setting_id"], "Custom Process")

    def test_stamps_instantiation_true_when_missing(self) -> None:
        result = profiles.materialize_process_import({
            "name": "Custom Process",
            "inherits": "0.20mm Standard @BBL A1M",
        })

        self.assertEqual(result["instantiation"], "true")

    def test_raises_when_inherits_parent_is_unknown(self) -> None:
        with self.assertRaises(profiles.ProfileNotFoundError):
            profiles.materialize_process_import({
                "name": "Orphan",
                "inherits": "Does Not Exist",
            })

    def test_does_not_stamp_print_settings_id(self) -> None:
        # The old function stamped print_settings_id=name; the new one
        # leaves the payload alone except for the documented fields.
        result = profiles.materialize_process_import({
            "name": "Custom Process",
            "inherits": "0.20mm Standard @BBL A1M",
        })
        # If the input did not have it, the output should not either.
        self.assertNotIn("print_settings_id", result)
```

If the file has tests that require `print_settings_id` to be stamped, those tests embody the old behavior; update them to NOT expect that field. Be careful: only update tests that asserted the merged/stamped behavior we are intentionally removing.

- [ ] **Step 3: Run the new tests to verify they fail**

```bash
.venv/bin/pytest tests/test_process_import.py::MaterializeProcessImportRawFormTests -v
```

Expected: most fail.

- [ ] **Step 4: Rewrite `materialize_process_import`**

Replace the body of `materialize_process_import` (`app/profiles.py:321-366`) with:

```python
def materialize_process_import(data: dict[str, Any]) -> dict[str, Any]:
    """Lightly stamp an imported process payload and return its raw form.

    The returned dict preserves `inherits` so the inheritance chain is
    resolved at slice / listing time. Stamping is limited to:

    - `setting_id`: synthesized from `name` if missing.
    - `instantiation`: set to `"true"` if missing.

    Validates that `inherits`, when set, points to a known process
    profile.
    """
    name = data.get("name")
    if not isinstance(name, str) or not name.strip():
        raise ValueError("Missing or invalid 'name' field.")
    name = name.strip()

    setting_id = data.get("setting_id", name)
    if not isinstance(setting_id, str) or not setting_id.strip():
        raise ValueError("Missing or invalid 'setting_id' field.")
    setting_id = setting_id.strip()

    inherits = data.get("inherits")
    if isinstance(inherits, str) and inherits.strip():
        parent_name = _resolve_process_parent_ref(inherits.strip())
        if not parent_name:
            raise ProfileNotFoundError(
                f"Process parent '{inherits.strip()}' not found"
            )

    result = dict(data)
    result["name"] = name
    result["setting_id"] = setting_id
    if "instantiation" not in result:
        result["instantiation"] = "true"
    return result
```

- [ ] **Step 5: Run all tests in `test_process_import.py`**

```bash
.venv/bin/pytest tests/test_process_import.py -v
```

Expected: tests in `MaterializeProcessImportRawFormTests` PASS. Existing tests that asserted merged or `print_settings_id`-stamped behavior will need to be updated. Update them inline to match the new behavior — e.g. an assertion that the output does NOT contain a parent-only key.

- [ ] **Step 6: Commit**

```bash
git add app/profiles.py tests/test_process_import.py
git commit -m "Rewrite materialize_process_import to keep inherits

- the saved process payload preserves inherits and is no longer merged with the parent at import time
- inheritance is resolved on read (listings, slicing, preview)"
```

---

## Task 9: Rename `resolved_payload` → `resolved_profile` and return merged form

**Files:**
- Modify: `app/models.py:89-108`.
- Modify: `app/main.py:188-267` (the two `resolve-import` endpoints).
- Modify: `tests/test_import_endpoints.py` — every reference to `resolved_payload`.

**Context:** The preview endpoint now returns the **merged** form (inheritance resolved in memory). The field is renamed because its meaning changed. Clients that previously POSTed `resolved_payload` back to `/profiles/...` should switch to POSTing their original raw payload (handled in Task 10 docs / spool-helper migration).

- [ ] **Step 1: Update `app/models.py`**

In `app/models.py`, replace the two preview classes:

```python
class FilamentProfileImportPreview(BaseModel):
    """Resolved filament profile preview before saving."""

    setting_id: str = Field(description="Profile identifier.")
    filament_id: str = Field(description="Filament identifier used for AMS assignment.")
    name: str = Field(description="Profile name.")
    filament_type: str = Field(description="Resolved filament material type.", examples=["PLA"])
    resolved_profile: dict = Field(
        description=(
            "Fully merged filament profile (inheritance resolved) — informational. "
            "Send the original raw payload to POST /profiles/filaments, not this field."
        ),
    )


class ProcessProfileImportPreview(BaseModel):
    """Resolved process profile preview before saving."""

    setting_id: str = Field(description="Profile identifier.")
    name: str = Field(description="Profile name.")
    inherits_resolved: str = Field(
        default="",
        description="Name of the parent profile that the import resolved against.",
    )
    resolved_profile: dict = Field(
        description=(
            "Fully merged process profile (inheritance resolved) — informational. "
            "Send the original raw payload to POST /profiles/processes, not this field."
        ),
    )
```

- [ ] **Step 2: Update `app/main.py` to populate `resolved_profile` with the merged form**

Find `resolve_filament_import` (`app/main.py:189-214`). Replace it with:

```python
@app.post(
    "/profiles/filaments/resolve-import",
    response_model=FilamentProfileImportPreview,
    tags=["Profiles"],
)
async def resolve_filament_import(request: Request):
    """Preview the materialized + resolved view of a filament import payload.

    The returned `resolved_profile` is the fully merged form
    (inheritance resolved in memory) for inspection. The saved form on
    POST is the raw payload — clients should POST their original
    upload to /profiles/filaments, not this preview's resolved_profile.
    """
    try:
        raw_data = await request.json()
    except Exception:
        return JSONResponse(status_code=400, content={"error": "Invalid JSON body."})

    data, error_response = _read_filament_import_body(raw_data)
    if error_response is not None or data is None:
        return error_response

    try:
        merged = _resolve_chain_for_payload(data, category="filament")
    except ProfileNotFoundError as exc:
        return JSONResponse(status_code=400, content={"error": str(exc)})

    filament_type = merged.get("filament_type", "")
    if isinstance(filament_type, list):
        filament_type = filament_type[0] if filament_type else ""

    return FilamentProfileImportPreview(
        setting_id=data["setting_id"],
        filament_id=str(merged.get("filament_id", "")),
        name=str(merged.get("name", "")),
        filament_type=str(filament_type or ""),
        resolved_profile=merged,
    )
```

The reference to `_resolve_chain_for_payload` requires importing this module-private function. Add it to the existing imports near the top of `main.py`. Find the line:

```python
from .profiles import (
    ...
    materialize_filament_import,
    materialize_process_import,
    ...
)
```

Add `_resolve_chain_for_payload` to that import list. (It is private by Python convention but the test file already imports private helpers from `app.profiles`; that pattern is acceptable here.)

Find `resolve_process_import` (`app/main.py:240-267`). Replace it with:

```python
@app.post(
    "/profiles/processes/resolve-import",
    response_model=ProcessProfileImportPreview,
    tags=["Profiles"],
)
async def resolve_process_import(request: Request):
    """Preview the materialized + resolved view of a process import payload."""
    try:
        raw_data = await request.json()
    except Exception:
        return JSONResponse(status_code=400, content={"error": "Invalid JSON body."})

    data, error_response = _read_process_import_body(raw_data)
    if error_response is not None or data is None:
        return error_response

    try:
        merged = _resolve_chain_for_payload(data, category="process")
    except ProfileNotFoundError as exc:
        return JSONResponse(status_code=400, content={"error": str(exc)})

    inherits_resolved = ""
    if isinstance(raw_data, dict):
        raw_inherits = raw_data.get("inherits")
        if isinstance(raw_inherits, str):
            inherits_resolved = raw_inherits.strip()

    return ProcessProfileImportPreview(
        setting_id=data["setting_id"],
        name=str(merged.get("name", "")),
        inherits_resolved=inherits_resolved,
        resolved_profile=merged,
    )
```

- [ ] **Step 3: Update existing tests in `tests/test_import_endpoints.py` that reference `resolved_payload`**

Replace every instance of `"resolved_payload"` with `"resolved_profile"` in the test file. Use:

```bash
grep -n "resolved_payload" tests/test_import_endpoints.py
```

For each match, rename the key. Tests that POST `preview["resolved_payload"]` back to the import endpoint should now POST the **original raw input body** (e.g. `body` rather than `preview["resolved_profile"]`). Read each test carefully and adjust the POST body accordingly:

- `test_resolve_import_returns_preview_with_resolved_payload` — rename the test, update the field name. The assertions that read merged values (e.g. `payload["layer_height"]`) should keep working because `resolved_profile` is the merged form.
- `test_save_writes_file_and_lists_under_user_filter` — change `json=preview["resolved_payload"]` to `json=body` (the original input).
- `CollisionSemanticsTests` — change `json=preview["resolved_payload"]` to `json=body` for the initial import. The "modified" payloads can keep using the merged form's keys but the safer rewrite is to start from `body` and add the modification.
- `ProcessDeleteEndpointTests.test_delete_user_process_removes_file_and_unlists` — change `json=preview["resolved_payload"]` to `json=body`.

For the `on_disk["from"] == "User"` and `on_disk["print_settings_id"] == preview["name"]` assertions in `test_save_writes_file_and_lists_under_user_filter`: these assert old stamping behavior. Replace them with assertions that match the new raw form:

```python
on_disk = json.loads(written_path.read_text())
self.assertEqual(on_disk["inherits"], "0.20mm Standard @BBL A1M")
self.assertEqual(on_disk["instantiation"], "true")
# from: "User" was already in the fixture, so it survives.
self.assertEqual(on_disk["from"], "User")
# print_settings_id is no longer stamped; it should match what the
# fixture supplied.
self.assertEqual(on_disk["print_settings_id"], "eSUN PLA-Basic @BBL A1M Process")
```

- [ ] **Step 4: Run the test_import_endpoints.py module**

```bash
.venv/bin/pytest tests/test_import_endpoints.py -v
```

Expected: all tests PASS. If any tests still fail, read each failure and align the assertion with the new behavior — the test file is the source of truth for what the public API does.

- [ ] **Step 5: Commit**

```bash
git add app/models.py app/main.py tests/test_import_endpoints.py
git commit -m "Rename resolved_payload to resolved_profile (merged form)

- preview endpoints now return the fully merged profile in resolved_profile, computed via _resolve_chain_for_payload
- callers should POST their original raw payload to /profiles/filaments and /profiles/processes instead of round-tripping through the preview"
```

---

## Task 10: Wire POST endpoints to derive response fields from the resolved chain

**Files:**
- Modify: `app/main.py` — `import_filament_profile` (around `:351-407`) and `import_process_profile` (around `:270-321`).
- Test: `tests/test_import_endpoints.py` (already updated in Task 9; add a few coverage cases here).

**Context:** Today's `import_filament_profile` reads `filament_id` and `filament_type` directly off the merged-on-import data. After Task 7, `data` returned by `materialize_filament_import` is the raw form — `filament_type` may be missing (inherited). The POST endpoint must resolve the chain after save to populate the response.

- [ ] **Step 1: Add a test asserting that the POST response contains `filament_type` from the parent**

Append to `tests/test_import_endpoints.py` inside an appropriate test class (or a new one):

```python
class FilamentImportResponseDerivedFieldsTests(_ProfileEndpointTestBase):
    def test_response_filament_type_comes_from_resolved_parent(self) -> None:
        # The fixture is a thin export inheriting Bambu PLA Basic @BBL A1M;
        # the parent declares filament_type=PLA. The thin import does not.
        body = json.loads((FIXTURE_DIR / "filament_esun_pla_basic_a1m.json").read_text())

        resp = self.client.post("/profiles/filaments", json=body)

        self.assertEqual(resp.status_code, 201)
        data = resp.json()
        self.assertEqual(data["filament_type"], "PLA")
        self.assertTrue(data["filament_id"])

    def test_saved_file_preserves_inherits_and_is_not_flattened(self) -> None:
        body = json.loads((FIXTURE_DIR / "filament_esun_pla_basic_a1m.json").read_text())
        self.client.post("/profiles/filaments", json=body)

        on_disk = json.loads(
            (self.user_dir / "eSUN PLA-Basic @BBL A1M.json").read_text()
        )
        self.assertEqual(on_disk["inherits"], "Bambu PLA Basic @BBL A1M")
        # filament_type is NOT merged into the saved file (it inherits).
        self.assertNotIn("filament_type", on_disk)
        # filament_id IS stamped (AMS identity).
        self.assertTrue(on_disk["filament_id"].startswith("P"))
```

- [ ] **Step 2: Run the new tests to verify they fail**

```bash
.venv/bin/pytest tests/test_import_endpoints.py::FilamentImportResponseDerivedFieldsTests -v
```

Expected: `test_response_filament_type_comes_from_resolved_parent` may fail because today's response reads `filament_type` from the (no-longer-merged) `data`. `test_saved_file_preserves_inherits_and_is_not_flattened` should already PASS after Tasks 7-9 if the POST endpoint saves whatever `materialize_filament_import` returns.

- [ ] **Step 3: Update `import_filament_profile` to read response fields from the resolved view**

Find `import_filament_profile` (`app/main.py:351-407`). Replace the response construction:

```python
    with open(file_path, "w") as f:
        json.dump(data, f, indent=2)

    load_all_profiles()

    # Derive filament_type / filament_id from the resolved chain after
    # save so thin imports report the parent's values to the client.
    try:
        resolved = get_profile("filament", setting_id)
    except ProfileNotFoundError:
        resolved = data

    filament_type = resolved.get("filament_type", "")
    if isinstance(filament_type, list):
        filament_type = filament_type[0] if filament_type else ""

    return JSONResponse(
        status_code=200 if exists else 201,
        content=FilamentProfileImportResponse(
            setting_id=setting_id,
            filament_id=str(resolved.get("filament_id", "")),
            name=str(resolved.get("name", "")),
            filament_type=filament_type,
            message=f"Profile '{str(resolved.get('name', ''))}' imported successfully.",
        ).model_dump(),
    )
```

Ensure `get_profile` and `ProfileNotFoundError` are already imported at the top of `main.py`. If not, add them to the existing import block.

- [ ] **Step 4: Update `import_process_profile` similarly**

Find `import_process_profile` (`app/main.py:270-321`). Update the response construction:

```python
    with open(file_path, "w") as f:
        json.dump(data, f, indent=2)

    load_all_profiles()

    try:
        resolved = get_profile("process", setting_id)
    except ProfileNotFoundError:
        resolved = data

    return JSONResponse(
        status_code=200 if exists else 201,
        content=ProcessProfileImportResponse(
            setting_id=setting_id,
            name=str(resolved.get("name", "")),
            message=f"Profile '{str(resolved.get('name', ''))}' imported successfully.",
        ).model_dump(),
    )
```

- [ ] **Step 5: Run the new tests to verify they pass**

```bash
.venv/bin/pytest tests/test_import_endpoints.py::FilamentImportResponseDerivedFieldsTests -v
```

Expected: both PASS.

- [ ] **Step 6: Run the full test suite for regressions**

```bash
.venv/bin/pytest tests/ -v
```

Expected: all tests pass.

- [ ] **Step 7: Commit**

```bash
git add app/main.py tests/test_import_endpoints.py
git commit -m "Derive POST response fields from the resolved chain

- import_filament_profile and import_process_profile read filament_type / filament_id / name off get_profile() after save, so thin imports report parent-inherited values back to the client even though the saved file is raw"
```

---

## Task 11: Add round-trip and backwards-compatibility tests

**Files:**
- Test: `tests/test_import_endpoints.py` (extend).

**Context:** Lock down the contract: a thin GUI export → POST → GET resolves correctly; an existing flattened user file (no `inherits`) keeps working.

- [ ] **Step 1: Add the round-trip tests**

Append to `tests/test_import_endpoints.py`:

```python
class FilamentImportRoundTripTests(_ProfileEndpointTestBase):
    def test_thin_export_round_trip_resolves_parent_values(self) -> None:
        body = json.loads((FIXTURE_DIR / "filament_esun_pla_basic_a1m.json").read_text())

        post = self.client.post("/profiles/filaments", json=body)
        self.assertEqual(post.status_code, 201)
        setting_id = post.json()["setting_id"]

        # GET resolves the chain and returns merged values.
        resolved = self.client.get(f"/profiles/filaments/{setting_id}").json()
        self.assertEqual(resolved["filament_type"], ["PLA"])
        # Stamped child filament_id wins over the parent's.
        self.assertEqual(resolved["filament_id"], post.json()["filament_id"])
        # Differential keys from the upload survive.
        self.assertEqual(resolved["nozzle_temperature"], ["230"])

    def test_flattened_legacy_user_profile_still_loads(self) -> None:
        # Simulate an already-flattened file written by the old
        # materializer: no inherits, parent values inlined.
        legacy = {
            "name": "Legacy Flat",
            "setting_id": "LEGACYFLAT",
            "from": "User",
            "instantiation": "true",
            "filament_id": "PLEGACY1",
            "filament_type": ["PLA"],
            "compatible_printers": ["Bambu Lab A1 mini 0.4 nozzle"],
            "nozzle_temperature": ["220"],
        }
        (self.user_dir / "LEGACYFLAT.json").write_text(json.dumps(legacy))
        profiles.load_all_profiles()

        listing = self.client.get("/profiles/filaments").json()
        names = {p["name"] for p in listing}
        self.assertIn("Legacy Flat", names)

        resolved = self.client.get("/profiles/filaments/LEGACYFLAT").json()
        self.assertEqual(resolved["filament_type"], ["PLA"])
        self.assertEqual(resolved["nozzle_temperature"], ["220"])
```

- [ ] **Step 2: Run the new tests**

```bash
.venv/bin/pytest tests/test_import_endpoints.py::FilamentImportRoundTripTests -v
```

Expected: both PASS (the implementation work is already done in Tasks 1-10; these tests lock in the contract).

- [ ] **Step 3: Run the full test suite one more time**

```bash
.venv/bin/pytest tests/ -v
```

Expected: all tests pass.

- [ ] **Step 4: Commit**

```bash
git add tests/test_import_endpoints.py
git commit -m "Add round-trip and legacy-file tests for filament import

- thin GUI export → POST → GET resolves to a fully merged view with parent values
- pre-existing flattened user files (no inherits) keep loading and resolving as before"
```

---

## Task 12: Bump `API_REVISION`

**Files:**
- Modify: `app/config.py:4`.

**Context:** The preview field rename `resolved_payload` → `resolved_profile` is a breaking change for any client reading the preview response. Bump the API revision so clients can detect.

- [ ] **Step 1: Edit `app/config.py`**

```python
ORCA_VERSION = "2.3.2"
API_REVISION = "13"
VERSION = f"{ORCA_VERSION}-{API_REVISION}"
```

- [ ] **Step 2: Run the health endpoint test (if any) and the full suite**

```bash
.venv/bin/pytest tests/ -v
```

Expected: all tests pass. If any test asserts the exact `VERSION` string, update it to the new revision.

- [ ] **Step 3: Commit**

```bash
git add app/config.py
git commit -m "Bump API revision to 2.3.2-13

- breaking change: /profiles/{filaments,processes}/resolve-import returns the merged profile in 'resolved_profile' (renamed from 'resolved_payload')
- saved profile files now preserve inherits; clients should POST the original raw payload to /profiles/{filaments,processes}, not the preview's resolved_profile"
```

---

## Self-review checklist

After completing all tasks, verify:

1. **Spec coverage:**
   - Raw form on save (filament + process) → Tasks 7, 8.
   - `filament_id` stamping → Task 7.
   - AMS-scope uniqueness check → Tasks 5, 6, 7.
   - Strict resolution → Task 1.
   - Listing-side wraps with logs → Tasks 2, 3.
   - `resolved_payload` → `resolved_profile` rename + merged form → Task 9.
   - POST accepts raw, response derives from resolved chain → Task 10.
   - Backwards compat (legacy flat files) → Task 11.
   - API_REVISION bump → Task 12.
   - Spec section "Client migration (bambu-spool-helper)" — out of scope here; tracked in spec for follow-up in the spool-helper repo.

2. **No placeholders left:** every step has concrete code or commands.

3. **Type / signature consistency:** `_resolve_chain_for_payload(payload, *, category)`, `_compatible_printers_set_for_payload(payload, *, category)`, `_check_filament_id_ams_scope(*, filament_id, compatible_printers, exclude_setting_id)`. Each helper is referenced consistently across tasks.

---

## Follow-up (out of scope for this plan)

In the bambu-spool-helper repo, update `app/routers/web.py`:

- Flow A (`web.py:1170-1213`): read `filament_type` from `resolved_preview["resolved_profile"]` (renamed); POST the original upload (`payload` before the rebind) to `/profiles/filaments`, not `resolved_payload`.
- Process flow (`web.py:1072-1082`): read `resolved_profile` if needed for inspection; POST the original upload to `/profiles/processes`.
- Field name change: `resolved_payload` → `resolved_profile` everywhere.

Flow B (`web.py:1296-1389`) needs no change — it already POSTs raw payloads with `inherits`.
