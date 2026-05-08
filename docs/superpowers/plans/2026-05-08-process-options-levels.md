# Process Option Levels Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the binary `process_allowlist.json` filter with a three-tier `basic` / `detailed` / `advanced` taxonomy that ships per-option in both `/options/process` and `/options/process/layout`.

**Architecture:** Each option's level is computed at startup from libslic3r's `ConfigOptionDef::mode` (already surfaced by `dump-options` as `simple`/`advanced`/`develop`) merged with a per-key override JSON (`app/process_levels.json`). The result lives inline on each option in both API responses; clients filter cumulatively (≤ selected level).

**Tech Stack:** Python 3.11 (FastAPI app), libslic3r C++ (already emits `mode` — no changes there), pytest.

**Spec:** `docs/superpowers/specs/2026-05-08-process-options-levels-design.md`

**Pre-discovery (verified before plan was written):**
- `dump-options` already emits `mode` per option at `cpp/src/dump_options_mode.cpp:184`. No C++ changes needed.
- Upstream enum is `comSimple` / `comAdvanced` / `comDevelop` (NOT `comDeveloper` as the spec text mentions). Mode strings are `"simple"` / `"advanced"` / `"develop"`. The spec's intent is unchanged; only the string literal differs.

**Files touched:**
- Create: `app/process_levels.json`
- Modify: `app/options.py` (replace allowlist filter with level-stamp merge)
- Modify: `app/main.py` (docstring on `GET /options/process/layout`)
- Modify: `tests/test_options_module.py` (replace filter assertions with level-stamp assertions)
- Rename: `scripts/check_allowlist.py` → `scripts/check_levels.py` (update field names + JSON path)
- Rename: `tests/test_check_allowlist.py` → `tests/test_check_levels.py`
- Modify: `docs/process-parameter-editor-api.md` (rewrite around levels)
- Delete: `app/process_allowlist.json`
- Revert (no commit): `app/config.py`, `app/main.py`, `app/options.py`, `tests/test_options_module.py` — drop the in-flight `PROCESS_ALLOWLIST_ENABLED` opt-in work.

---

## Task 1: Discard in-flight `PROCESS_ALLOWLIST_ENABLED` work

The earlier session left an opt-in flag in working copy. The levels system supersedes it; revert before building.

**Files:**
- Restore: `app/config.py`, `app/main.py`, `app/options.py`, `tests/test_options_module.py` (to HEAD)

- [ ] **Step 1: Verify what's about to be reverted**

```bash
git status --short app/config.py app/main.py app/options.py tests/test_options_module.py
```

Expected: 4 files showing ` M`.

- [ ] **Step 2: Restore to HEAD**

```bash
git restore app/config.py app/main.py app/options.py tests/test_options_module.py
```

- [ ] **Step 3: Confirm clean working copy on those files**

```bash
git status --short app/ tests/
```

Expected: nothing on those paths. Untracked preview-emit files may still appear; ignore (they're stashed).

- [ ] **Step 4: Confirm `cfg.PROCESS_ALLOWLIST_ENABLED` is gone**

```bash
grep -n PROCESS_ALLOWLIST_ENABLED app/ tests/ -r
```

Expected: no matches.

No commit; this is a baseline reset.

---

## Task 2: Seed `app/process_levels.json`

The override file. Seeded with the 12 keys from the old allowlist, all stamped `basic`. Anything not listed inherits its level from upstream `mode`.

**Files:**
- Create: `app/process_levels.json`

- [ ] **Step 1: Write the file**

```json
{
  "revision": "2026-05-08.1",
  "overrides": {
    "layer_height": "basic",
    "initial_layer_print_height": "basic",
    "wall_loops": "basic",
    "top_shell_layers": "basic",
    "bottom_shell_layers": "basic",
    "sparse_infill_density": "basic",
    "sparse_infill_pattern": "basic",
    "enable_support": "basic",
    "support_type": "basic",
    "support_threshold_angle": "basic",
    "brim_type": "basic",
    "brim_width": "basic"
  }
}
```

- [ ] **Step 2: Validate JSON parses**

```bash
python3 -c "import json; print(len(json.load(open('app/process_levels.json'))['overrides']))"
```

Expected: `12`.

- [ ] **Step 3: Commit**

```bash
git add app/process_levels.json
git commit -m "Add process_levels.json — 12 basic-tier override keys"
```

---

## Task 3: Add `_resolve_level()` helper (TDD)

Pure function: maps an option's upstream `mode` string + an overrides dict into one of `basic` / `detailed` / `advanced`. Lives in `app/options.py` next to existing helpers.

**Files:**
- Modify: `app/options.py` (add `_MODE_TO_LEVEL` constant + `_resolve_level()` function)
- Modify: `tests/test_options_module.py` (add unit tests)

- [ ] **Step 1: Write failing tests**

Append to `tests/test_options_module.py`:

```python
def test_resolve_level_simple_maps_to_basic() -> None:
    assert options._resolve_level("layer_height", "simple", {}) == "basic"


def test_resolve_level_advanced_maps_to_detailed() -> None:
    assert options._resolve_level("seam_position", "advanced", {}) == "detailed"


def test_resolve_level_develop_maps_to_advanced() -> None:
    assert options._resolve_level("xy_compensation", "develop", {}) == "advanced"


def test_resolve_level_missing_mode_defaults_to_advanced() -> None:
    # Some print_config_def.cpp options have no mode tag; libslic3r exposes
    # them as the default ("simple" — comSimple is enum value 0). For ones
    # the dump emits as "unknown" or empty, default to advanced.
    assert options._resolve_level("internal_thing", "unknown", {}) == "advanced"
    assert options._resolve_level("internal_thing", "", {}) == "advanced"


def test_resolve_level_override_wins_over_upstream() -> None:
    overrides = {"wall_loops": "basic"}
    # wall_loops is comAdvanced upstream → would be "detailed", but override
    # promotes it.
    assert options._resolve_level("wall_loops", "advanced", overrides) == "basic"


def test_resolve_level_override_can_demote() -> None:
    overrides = {"layer_height": "detailed"}
    # layer_height is comSimple upstream → would be "basic", override demotes.
    assert options._resolve_level("layer_height", "simple", overrides) == "detailed"
```

- [ ] **Step 2: Run tests, verify they fail**

```bash
docker exec orcaslicer-cli-orcaslicer-cli-1 sh -c "cd /app && python3 -m pytest tests/test_options_module.py -v -k resolve_level"
```

Expected: all 6 tests FAIL with `AttributeError: module 'app.options' has no attribute '_resolve_level'`.

- [ ] **Step 3: Implement helper**

Add to `app/options.py` near the top of the module (before `_build_metadata`):

```python
# Map upstream `ConfigOptionDef::mode` strings (emitted by dump-options at
# cpp/src/dump_options_mode.cpp:49-56) to our three-tier taxonomy.
_MODE_TO_LEVEL = {
    "simple": "basic",
    "advanced": "detailed",
    "develop": "advanced",
}


def _resolve_level(
    key: str,
    upstream_mode: str,
    overrides: dict[str, str],
) -> str:
    """Pick the level for a single option.

    Override JSON wins; otherwise map upstream mode; otherwise default to
    `advanced` (so engine-internal keys with missing/unknown mode tags are
    still reachable for power users via the modified view).
    """
    if key in overrides:
        return overrides[key]
    return _MODE_TO_LEVEL.get(upstream_mode, "advanced")
```

- [ ] **Step 4: Run tests, verify they pass**

```bash
docker exec orcaslicer-cli-orcaslicer-cli-1 sh -c "cd /app && python3 -m pytest tests/test_options_module.py -v -k resolve_level"
```

Expected: 6 PASSED.

- [ ] **Step 5: Commit**

```bash
git add app/options.py tests/test_options_module.py
git commit -m "Add _resolve_level: mode + overrides → basic/detailed/advanced"
```

---

## Task 4: Stamp `level` on the metadata catalogue (TDD)

`_build_metadata` currently reshapes the dump-options catalogue into a dict keyed by option key, passing through every field (including `mode`). Add a `level` field per option using `_resolve_level`.

**Files:**
- Modify: `app/options.py` (`_build_metadata` signature + body)
- Modify: `tests/test_options_module.py`

- [ ] **Step 1: Write failing test**

Append to `tests/test_options_module.py`:

```python
def test_build_metadata_stamps_level_per_option() -> None:
    catalogue = {
        "options": [
            {"key": "layer_height", "label": "Layer height", "mode": "simple"},
            {"key": "seam_position", "label": "Seam", "mode": "advanced"},
            {"key": "xy_compensation", "label": "XY", "mode": "develop"},
        ],
    }
    overrides = {"layer_height": "basic"}  # passes through as override
    metadata = options._build_metadata(catalogue, "test-version", overrides)

    assert metadata["options"]["layer_height"]["level"] == "basic"
    assert metadata["options"]["seam_position"]["level"] == "detailed"
    assert metadata["options"]["xy_compensation"]["level"] == "advanced"


def test_build_metadata_carries_levels_revision() -> None:
    metadata = options._build_metadata(
        {"options": []}, "test-version", {}, levels_revision="rev-1",
    )
    assert metadata["levels_revision"] == "rev-1"
```

- [ ] **Step 2: Run tests, verify they fail**

```bash
docker exec orcaslicer-cli-orcaslicer-cli-1 sh -c "cd /app && python3 -m pytest tests/test_options_module.py -v -k build_metadata"
```

Expected: FAIL — signature mismatch (current `_build_metadata` takes 2 args, new tests pass 3 or 4).

- [ ] **Step 3: Update `_build_metadata`**

Replace the existing `_build_metadata` in `app/options.py` (currently lines 77-87):

```python
def _build_metadata(
    catalogue: dict[str, Any],
    api_version: str,
    overrides: dict[str, str],
    *,
    levels_revision: str = "",
) -> dict[str, Any]:
    """Reshape the dump-options catalogue into the /options/process payload.

    Each option is stamped with `level` (basic/detailed/advanced) computed
    from its upstream `mode` and the overrides dict.
    """
    options_out: dict[str, dict[str, Any]] = {}
    for opt in catalogue.get("options", []):
        enriched = dict(opt)
        enriched["level"] = _resolve_level(
            opt["key"], opt.get("mode", ""), overrides,
        )
        options_out[opt["key"]] = enriched
    return {
        "version": api_version,
        "levels_revision": levels_revision,
        "options": options_out,
    }
```

- [ ] **Step 4: Run tests**

```bash
docker exec orcaslicer-cli-orcaslicer-cli-1 sh -c "cd /app && python3 -m pytest tests/test_options_module.py -v -k build_metadata"
```

Expected: 2 PASSED.

- [ ] **Step 5: Run the full options test module to find collateral failures**

```bash
docker exec orcaslicer-cli-orcaslicer-cli-1 sh -c "cd /app && python3 -m pytest tests/test_options_module.py -v"
```

Expected: some failures in `load_options_cache` tests (because the call site still passes 2 args to `_build_metadata`). Those are addressed in Task 6. Continue.

- [ ] **Step 6: Commit**

```bash
git add app/options.py tests/test_options_module.py
git commit -m "Stamp `level` per option in /options/process catalogue"
```

---

## Task 5: Stamp `level` on the paged layout (TDD)

Each option in `/options/process/layout` becomes `{"key": "...", "level": "..."}` (was a bare string). No filtering — clients filter cumulatively client-side.

**Files:**
- Modify: `app/options.py` (`_build_layout` signature + body, drop `filter_layout` use of allowlist)
- Modify: `tests/test_options_module.py`

- [ ] **Step 1: Write failing tests**

Append to `tests/test_options_module.py`:

```python
def test_build_layout_options_become_objects_with_level() -> None:
    layout_doc = {
        "pages": [{
            "label": "Quality",
            "optgroups": [{
                "label": "Layer height",
                "options": ["layer_height", "initial_layer_print_height"],
            }],
        }],
    }
    metadata_index = {
        "layer_height": {"level": "basic"},
        "initial_layer_print_height": {"level": "basic"},
    }
    layout = options._build_layout(layout_doc, metadata_index, "test-version", "rev-1")

    page = layout["pages"][0]
    optgroup = page["optgroups"][0]
    assert optgroup["options"] == [
        {"key": "layer_height", "level": "basic"},
        {"key": "initial_layer_print_height", "level": "basic"},
    ]


def test_build_layout_preserves_pages_optgroups_order() -> None:
    layout_doc = {
        "pages": [
            {"label": "Quality", "optgroups": [
                {"label": "A", "options": ["k1"]},
                {"label": "B", "options": ["k2"]},
            ]},
            {"label": "Strength", "optgroups": [
                {"label": "C", "options": ["k3"]},
            ]},
        ],
    }
    metadata_index = {k: {"level": "advanced"} for k in ["k1", "k2", "k3"]}
    layout = options._build_layout(layout_doc, metadata_index, "v", "r")

    assert [p["label"] for p in layout["pages"]] == ["Quality", "Strength"]
    assert [og["label"] for og in layout["pages"][0]["optgroups"]] == ["A", "B"]


def test_build_layout_unknown_key_defaults_to_advanced() -> None:
    # Tab.cpp regex caught a key that's not in dump-options output. Stamp
    # "advanced" so the option still renders rather than dropping it
    # silently — drift is the responsibility of scripts/check_levels.py.
    layout_doc = {
        "pages": [{"label": "P", "optgroups": [{
            "label": "G", "options": ["unknown_key"],
        }]}],
    }
    layout = options._build_layout(layout_doc, {}, "v", "r")
    assert layout["pages"][0]["optgroups"][0]["options"] == [
        {"key": "unknown_key", "level": "advanced"},
    ]


def test_build_layout_carries_levels_revision_and_drops_allowlist_revision() -> None:
    layout = options._build_layout({"pages": []}, {}, "v", "rev-7")
    assert layout["levels_revision"] == "rev-7"
    assert "allowlist_revision" not in layout
```

- [ ] **Step 2: Run tests, verify they fail**

```bash
docker exec orcaslicer-cli-orcaslicer-cli-1 sh -c "cd /app && python3 -m pytest tests/test_options_module.py -v -k build_layout"
```

Expected: FAIL — signature mismatch (current `_build_layout` takes `allowlist_doc`).

- [ ] **Step 3: Replace `_build_layout`**

Replace the existing `_build_layout` in `app/options.py` (currently lines 90-111):

```python
def _build_layout(
    layout_doc: dict[str, Any],
    metadata_options: dict[str, dict[str, Any]],
    api_version: str,
    levels_revision: str,
) -> dict[str, Any]:
    """Stamp `level` per option, preserving page/optgroup order.

    `metadata_options` is the per-key dict produced by `_build_metadata`
    — we read each option's level from there. Keys absent from the
    metadata catalogue (drift) are stamped `advanced` and pass through;
    `scripts/check_levels.py` is the strict gate that fails CI on drift.
    """
    pages_out: list[dict[str, Any]] = []
    for page in layout_doc.get("pages", []):
        new_optgroups = []
        for og in page.get("optgroups", []):
            new_options = [
                {
                    "key": key,
                    "level": metadata_options.get(key, {}).get("level", "advanced"),
                }
                for key in og.get("options", [])
            ]
            new_optgroups.append({**og, "options": new_options})
        pages_out.append({**page, "optgroups": new_optgroups})

    return {
        "version": api_version,
        "levels_revision": levels_revision,
        "pages": pages_out,
    }
```

- [ ] **Step 4: Drop `filter_layout` (unused after this task)**

Search for `filter_layout` to make sure no other site uses it:

```bash
grep -rn filter_layout app/ tests/ scripts/
```

If only `app/options.py` defines it and `tests/test_options_module.py` tests it, delete the function from `app/options.py` and the three filter tests from `tests/test_options_module.py` (`test_filter_layout_drops_non_allowlisted_options`, `test_filter_layout_drops_empty_optgroups_and_pages`, `test_filter_layout_preserves_option_order_within_optgroup`). Also delete the `fake_allowlist` fixture (lines around 43-58 of `test_options_module.py`) — replace with `fake_levels` in Task 6.

- [ ] **Step 5: Run tests**

```bash
docker exec orcaslicer-cli-orcaslicer-cli-1 sh -c "cd /app && python3 -m pytest tests/test_options_module.py -v -k build_layout"
```

Expected: 4 PASSED.

- [ ] **Step 6: Commit**

```bash
git add app/options.py tests/test_options_module.py
git commit -m "Layout endpoint emits {key, level} per option (no filtering)"
```

---

## Task 6: Wire `load_options_cache` to read `process_levels.json`

End-to-end glue: load the override JSON at startup, thread it through `_build_metadata` and `_build_layout`. Updates the integration test that exercises `load_options_cache`. Removes the orphaned allowlist references.

**Files:**
- Modify: `app/options.py` (module-level path constant, `load_options_cache` body)
- Modify: `tests/test_options_module.py` (replace `fake_allowlist` with `fake_levels`, fix `test_load_into_cache_*`)
- Delete: `app/process_allowlist.json`

- [ ] **Step 1: Write failing test for end-to-end load**

Replace the existing `test_load_into_cache_*` tests in `tests/test_options_module.py` (around lines 113-160) with:

```python
@pytest.fixture
def fake_levels(tmp_path: Path) -> Path:
    p = tmp_path / "process_levels.json"
    p.write_text(json.dumps({
        "revision": "test-rev-1",
        "overrides": {"layer_height": "basic", "wall_loops": "basic"},
    }))
    return p


async def test_load_options_cache_stamps_level_in_catalogue_and_layout(
    monkeypatch, fake_layout: Path, fake_levels: Path, fake_catalogue: dict,
) -> None:
    monkeypatch.setattr(options, "_LAYOUT_PATH", fake_layout)
    monkeypatch.setattr(options, "_LEVELS_PATH", fake_levels)

    fake_client = AsyncMock()
    fake_client.dump_options = AsyncMock(return_value=fake_catalogue)
    cache = await options.load_options_cache(binary_client=fake_client)

    # Catalogue: every option has a level.
    metadata = cache.metadata
    assert metadata["levels_revision"] == "test-rev-1"
    assert metadata["options"]["layer_height"]["level"] == "basic"
    # `seam_position` is `advanced` mode upstream in fake_catalogue → detailed.
    assert metadata["options"]["seam_position"]["level"] == "detailed"

    # Layout: every option is {key, level}; full Tab.cpp shape preserved.
    layout = cache.layout
    assert layout["levels_revision"] == "test-rev-1"
    page_labels = [p["label"] for p in layout["pages"]]
    assert page_labels == ["Quality", "Strength"]
    layer_options = layout["pages"][0]["optgroups"][0]["options"]
    assert layer_options == [
        {"key": "layer_height", "level": "basic"},
        {"key": "initial_layer_print_height", "level": "basic"},  # upstream simple
    ]


async def test_load_options_cache_warns_on_unknown_override_key(
    monkeypatch, tmp_path: Path, fake_layout: Path, fake_catalogue: dict, caplog,
) -> None:
    bad_levels = tmp_path / "process_levels.json"
    bad_levels.write_text(json.dumps({
        "revision": "test-rev-2",
        "overrides": {"this_key_does_not_exist": "basic"},
    }))
    monkeypatch.setattr(options, "_LAYOUT_PATH", fake_layout)
    monkeypatch.setattr(options, "_LEVELS_PATH", bad_levels)

    fake_client = AsyncMock()
    fake_client.dump_options = AsyncMock(return_value=fake_catalogue)
    with caplog.at_level("WARNING"):
        await options.load_options_cache(binary_client=fake_client)

    assert any(
        "this_key_does_not_exist" in record.message
        for record in caplog.records
    )
```

Update the `fake_catalogue` fixture (around line 60 of test_options_module.py) to include a `mode` field per option if it doesn't already:

```python
@pytest.fixture
def fake_catalogue() -> dict:
    return {
        "options": [
            {"key": "layer_height", "label": "Layer height",
             "type": "float", "mode": "simple"},
            {"key": "initial_layer_print_height", "label": "Initial layer",
             "type": "float", "mode": "simple"},
            {"key": "seam_position", "label": "Seam position",
             "type": "enum", "mode": "advanced"},
            {"key": "wall_loops", "label": "Wall loops",
             "type": "int", "mode": "advanced"},
        ],
    }
```

(If the existing fixture already has these keys but missing `mode`, just add `"mode"` per entry; matching values: `layer_height` → `"simple"`, `initial_layer_print_height` → `"simple"`, `seam_position` → `"advanced"`, `wall_loops` → `"advanced"`.)

- [ ] **Step 2: Run tests, verify they fail**

```bash
docker exec orcaslicer-cli-orcaslicer-cli-1 sh -c "cd /app && python3 -m pytest tests/test_options_module.py -v -k load_options_cache"
```

Expected: FAIL — `_LEVELS_PATH` doesn't exist in `app/options`.

- [ ] **Step 3: Replace `load_options_cache`**

Edit `app/options.py`. Near the top (with the existing `_LAYOUT_PATH` definition), update:

```python
_LAYOUT_PATH = Path(__file__).resolve().parent.parent / "cpp" / "src" / "generated" / "process_pages.json"
_LEVELS_PATH = Path(__file__).resolve().parent / "process_levels.json"
```

Remove the old `_ALLOWLIST_PATH = ...` line if it's still there.

Replace `load_options_cache` body (currently around lines 114-159) with:

```python
async def load_options_cache(*, binary_client: BinaryClient) -> OptionsCache:
    """Refresh the module-level cache. Call from app startup and /reload."""
    api_version = f"{cfg.ORCA_VERSION}-{cfg.API_REVISION}"

    catalogue = await binary_client.dump_options()

    if not _LAYOUT_PATH.exists():
        raise RuntimeError(
            f"process_pages.json missing at {_LAYOUT_PATH}; "
            "run scripts/extract_tab_layout.py")
    layout_doc = json.loads(_LAYOUT_PATH.read_text())

    if not _LEVELS_PATH.exists():
        raise RuntimeError(
            f"process_levels.json missing at {_LEVELS_PATH}")
    levels_doc = json.loads(_LEVELS_PATH.read_text())
    overrides = levels_doc.get("overrides", {})
    levels_revision = levels_doc.get("revision", "")

    metadata = _build_metadata(
        catalogue, api_version, overrides,
        levels_revision=levels_revision,
    )
    layout = _build_layout(
        layout_doc, metadata["options"], api_version, levels_revision,
    )

    # Drop a warning for any override key not in the metadata catalogue
    # (typo, removed upstream, filament/machine-domain leak). The strict
    # CI gate is scripts/check_levels.py; here we only log so a curated
    # mistake doesn't crash startup.
    metadata_keys = set(metadata["options"].keys())
    for key in overrides:
        if key not in metadata_keys:
            logger.warning(
                "process_levels.json references key %r which is not in "
                "dump-options (typo, removed upstream, or non-process key)",
                key)

    _cache.metadata = metadata
    _cache.layout = layout
    exposed_keys = sum(
        len(og["options"]) for p in layout["pages"] for og in p["optgroups"])
    logger.info(
        "options cache loaded: %d metadata entries, %d layout keys across "
        "%d pages, levels_revision=%s",
        len(metadata["options"]),
        exposed_keys,
        len(layout["pages"]),
        levels_revision,
    )
    return _cache
```

Update the module docstring at the top of `app/options.py` to drop the allowlist mention. Replace the existing description block (lines 1-19 area) with:

```python
"""Process-options metadata + layout cache.

Loads two pieces of data at startup (and on /profiles/reload):

  1. The full per-option metadata catalogue, by shelling out to
     ``orca-headless dump-options``. Served at ``GET /options/process``.

  2. The page → optgroup → option layout extracted at build time from
     ``Tab.cpp::TabPrint::build()`` (lives at
     ``cpp/src/generated/process_pages.json``). Served at
     ``GET /options/process/layout``.

Each option in both responses is stamped with a `level` field
(``basic`` / ``detailed`` / ``advanced``) computed from libslic3r's
upstream ``ConfigOptionDef::mode`` plus our per-key overrides in
``app/process_levels.json``. Clients filter cumulatively (≤ selected).
"""
```

- [ ] **Step 4: Run tests**

```bash
docker exec orcaslicer-cli-orcaslicer-cli-1 sh -c "cd /app && python3 -m pytest tests/test_options_module.py -v"
```

Expected: ALL PASSED.

- [ ] **Step 5: Delete the obsolete allowlist file**

```bash
git rm app/process_allowlist.json
```

- [ ] **Step 6: Commit**

```bash
git add app/options.py tests/test_options_module.py
git commit -m "Wire process_levels.json through load_options_cache; drop allowlist"
```

---

## Task 7: Update `/options/process/layout` endpoint docstring

The FastAPI endpoint's docstring still mentions the allowlist. Bring it in line.

**Files:**
- Modify: `app/main.py` (around the `get_process_options_layout` function)

- [ ] **Step 1: Find the docstring**

```bash
grep -n "Filtered server-side\|allowlist\|process/layout" app/main.py
```

- [ ] **Step 2: Replace it**

Edit the function's docstring (the one around line 705-712 of `app/main.py`):

```python
async def get_process_options_layout() -> dict[str, Any]:
    """Page → optgroup → option layout for the process editor's All view.

    Layout sourced from cpp/src/generated/process_pages.json (extracted at
    build time from Tab.cpp::TabPrint::build()). Each option is stamped
    with a `level` field (basic/detailed/advanced) merged from libslic3r's
    upstream `ConfigOptionDef::mode` and per-key overrides in
    app/process_levels.json. No server-side filtering — clients filter
    cumulatively by the user's selected level.
    """
```

- [ ] **Step 3: Verify nothing else in `app/main.py` references allowlist**

```bash
grep -n allowlist app/main.py
```

Expected: no matches.

- [ ] **Step 4: Commit**

```bash
git add app/main.py
git commit -m "Endpoint docstring: levels framing replaces allowlist"
```

---

## Task 8: Rename `check_allowlist` → `check_levels`

Drift detection: same three checks, applied to the levels overrides instead of the allowlist.

**Files:**
- Rename: `scripts/check_allowlist.py` → `scripts/check_levels.py`
- Rename: `tests/test_check_allowlist.py` → `tests/test_check_levels.py`

- [ ] **Step 1: Move the files**

```bash
git mv scripts/check_allowlist.py scripts/check_levels.py
git mv tests/test_check_allowlist.py tests/test_check_levels.py
```

- [ ] **Step 2: Update `scripts/check_levels.py`**

Replace the constant + variable names. Edit the file:

- `DEFAULT_ALLOWLIST = REPO_ROOT / "app" / "process_allowlist.json"` → `DEFAULT_LEVELS = REPO_ROOT / "app" / "process_levels.json"`
- Any function/variable named `allowlist` (e.g. `check_drift`'s parameters, internal locals) → `levels` or `overrides`.
- The JSON read shape: was `{"options": [...]}` (flat list), now `{"overrides": {key: level, ...}}` (dict). Adjust the key extraction:
  - Was: `keys = set(json.load(f)["options"])`
  - Now: `keys = set(json.load(f).get("overrides", {}).keys())`

The three drift checks themselves are unchanged in semantics — only the "set of keys we're checking" comes from a different shape.

- [ ] **Step 3: Update `tests/test_check_levels.py`**

- Module import: `from scripts.check_allowlist import check_drift` → `from scripts.check_levels import check_drift`
- Any inline test fixture that builds an allowlist JSON: change shape from `{"options": [...]}` to `{"revision": "...", "overrides": {key: level, ...}}`.

- [ ] **Step 4: Update the module docstring at the top of `scripts/check_levels.py`**

Reword to mention "process_levels.json" and "override keys" instead of "allowlist keys". The three drift modes stay; only the noun changes.

- [ ] **Step 5: Run the renamed test**

```bash
docker exec orcaslicer-cli-orcaslicer-cli-1 sh -c "cd /app && python3 -m pytest tests/test_check_levels.py -v"
```

Expected: ALL PASSED.

- [ ] **Step 6: Commit**

```bash
git add scripts/check_levels.py tests/test_check_levels.py
git commit -m "Rename check_allowlist → check_levels; read process_levels.json"
```

---

## Task 9: Update `docs/process-parameter-editor-api.md`

Doc was written around the allowlist. Rewrite the relevant sections around levels.

**Files:**
- Modify: `docs/process-parameter-editor-api.md`

- [ ] **Step 1: Identify the sections to rewrite**

Sections (from earlier inspection):
- "## At a glance" (table referencing allowlist)
- "## `GET /options/process/layout` — paged editor layout"
- "### Allowlist semantics" (entire subsection)
- "### Modified-view rendering recipe"
- "## Limitations and non-goals (v1)"

- [ ] **Step 2: Rewrite the table row in "At a glance"**

Find the table row for `/options/process/layout`:

> `| /options/process/layout | GET | Page → optgroup → option layout, allowlist-filtered. |`

Replace with:

> `| /options/process/layout | GET | Page → optgroup → option layout. Each option carries a level (basic/detailed/advanced); clients filter cumulatively. |`

And update the row for `/options/process` to mention levels:

> `| /options/process | GET | Per-option metadata catalogue, with level stamped per option. Unfiltered. |`

- [ ] **Step 3: Replace the `/options/process/layout` section body**

Find the section header and rewrite the body up to (not including) the next `##` header. Use this as the rewrite:

```markdown
## `GET /options/process/layout` — paged editor layout

Returns the page → optgroup → option layout extracted at build time from
`Tab.cpp::TabPrint::build()`. Each option carries a `level` field merged
from libslic3r's upstream `ConfigOptionDef::mode` and our per-key
overrides in `app/process_levels.json`.

```json
{
  "version": "2.3.2-46",
  "levels_revision": "2026-05-08.1",
  "pages": [
    {
      "label": "Quality",
      "optgroups": [
        {
          "label": "Layer height",
          "options": [
            {"key": "layer_height", "level": "basic"},
            {"key": "initial_layer_print_height", "level": "basic"}
          ]
        }
      ]
    }
  ]
}
```

### Levels semantics

Three tiers, addressed by stable IDs (display copy is the client's
concern):

| ID         | Default source              | Intent                                      |
|------------|-----------------------------|---------------------------------------------|
| `basic`    | `comSimple` + overrides     | Small set; "I just want to print."          |
| `detailed` | `comAdvanced` + overrides   | Most knobs most users will want.            |
| `advanced` | `comDevelop` + overrides    | Everything; engine internals included.      |

**Cumulative filter.** `basic ⊆ detailed ⊆ advanced`. Client filters
"show options at level ≤ selected." Picking *Advanced* shows everything.

**Two cache keys:** `version` (binary build) and `levels_revision`
(`app/process_levels.json` revision). Bust the layout cache on either
change.
```

- [ ] **Step 4: Update the modified-view rendering recipe**

Find the section "### Modified-view rendering recipe". The old steps reference allowlist membership. Replace the relevant steps:

> 3. If the key is allowlisted (present in step 2): render an editable widget. ...
> 4. If not allowlisted: render the same widget read-only ...

with:

> 3. Render an editable widget at any level the user has selected access to (key's `level` ≤ selected level). Initial value is `value`. On change, add to the `process_overrides` payload sent at slice time.
> 4. For keys above the selected level (e.g. `level: "advanced"` while user is on `basic`): render the widget read-only with the value displayed. No edit. The user can promote their level to unlock.

- [ ] **Step 5: Update "Limitations and non-goals (v1)"**

Drop any bullet that says "the allowlist starts small and grows toward 'everything'" — replace with a bullet about how levels evolve:

> - The Basic tier starts small (~12 keys); curating the Basic/Detailed split is iterative product work and only requires editing `app/process_levels.json` (no rebuild). The Advanced tier always exposes every process key libslic3r knows.

- [ ] **Step 6: Search for any leftover "allowlist" references**

```bash
grep -n -i allowlist docs/process-parameter-editor-api.md
```

Resolve each remaining reference: either reword to "level overrides" / "levels JSON", or delete if obsolete.

- [ ] **Step 7: Commit**

```bash
git add docs/process-parameter-editor-api.md
git commit -m "docs: rewrite process-parameter-editor-api around levels"
```

---

## Task 10: Live integration verification

Hot-reload the FastAPI app (or restart the container) so it picks up the new `process_levels.json` and the rewritten `app/options.py`. Hit the two endpoints with curl and assert the response shape.

**Files:**
- None (manual verification)

- [ ] **Step 1: Trigger the in-process reload**

```bash
curl -sf -X POST http://localhost:8070/profiles/reload
```

Expected: `{"status": "..."}` 200. Watch the container's stderr for the new log line:

```bash
docker logs --tail 5 orcaslicer-cli-orcaslicer-cli-1 2>&1 | grep "options cache"
```

Expected: a line ending in `levels_revision=2026-05-08.1`.

- [ ] **Step 2: Verify catalogue carries `level`**

```bash
curl -sf http://localhost:8070/options/process | python3 -c "
import json, sys
d = json.load(sys.stdin)
opts = d['options']
print('levels_revision:', d.get('levels_revision'))
print('layer_height level:', opts['layer_height'].get('level'))
print('seam_position level:', opts['seam_position'].get('level'))
print('total options:', len(opts))
levels_seen = set(o.get('level') for o in opts.values())
print('levels seen:', sorted(levels_seen))
"
```

Expected:
- `levels_revision: 2026-05-08.1`
- `layer_height level: basic`
- `seam_position level: detailed`
- `total options: ~600`
- `levels seen: ['advanced', 'basic', 'detailed']`

- [ ] **Step 3: Verify layout has `{key, level}` objects**

```bash
curl -sf http://localhost:8070/options/process/layout | python3 -c "
import json, sys
d = json.load(sys.stdin)
print('levels_revision:', d.get('levels_revision'))
print('first page:', d['pages'][0]['label'])
print('first option:', d['pages'][0]['optgroups'][0]['options'][0])
import collections
counts = collections.Counter()
for p in d['pages']:
    for og in p['optgroups']:
        for o in og['options']:
            counts[o['level']] += 1
print('per-level counts:', dict(counts))
"
```

Expected:
- `levels_revision: 2026-05-08.1`
- `first page: Quality`
- `first option: {'key': 'layer_height', 'level': 'basic'}`
- `per-level counts:` 12 basic, plus detailed and advanced totals from upstream `mode` mapping.

- [ ] **Step 4: Run the drift checker**

```bash
docker exec orcaslicer-cli-orcaslicer-cli-1 sh -c "cd /app && python3 scripts/check_levels.py --check"
```

Expected: exit 0 ("no drift detected" or equivalent). If drift is reported, fix in the levels JSON or layout source per the script's output.

- [ ] **Step 5: No commit; this is verification only**

If any assertion above fails, return to the relevant earlier task and fix; do not skip.

---

## Self-Review

**Spec coverage** (cross-checked against `docs/superpowers/specs/2026-05-08-process-options-levels-design.md`):

| Spec section                          | Covered by task |
|---------------------------------------|-----------------|
| Level taxonomy + cumulative semantic  | Task 3, 9       |
| Override file (`process_levels.json`) | Task 2          |
| Mode → level mapping helper           | Task 3          |
| `/options/process` carries `level`    | Task 4          |
| `/options/process/layout` `{key, level}` | Task 5       |
| `_LEVELS_PATH` load + warn on drift   | Task 6          |
| Drop in-flight `PROCESS_ALLOWLIST_ENABLED` | Task 1     |
| Delete `process_allowlist.json`       | Task 6          |
| Rename `check_allowlist` → `check_levels` | Task 8       |
| API doc rewrite                       | Task 9          |
| Endpoint docstring update             | Task 7          |
| Caching keys (`version` + `levels_revision`) | Task 4, 5, 6 |

**Placeholder scan:** Every step contains the actual code or command needed. No "TBD" / "implement appropriately" / "etc." references.

**Type consistency:**
- `_resolve_level(key, upstream_mode, overrides) → str` — used identically in tasks 3, 4, 6.
- `_build_metadata(catalogue, api_version, overrides, *, levels_revision="") → dict` — signature aligned across tasks 4 and 6.
- `_build_layout(layout_doc, metadata_options, api_version, levels_revision) → dict` — signature aligned across tasks 5 and 6.
- `_LEVELS_PATH` constant introduced in Task 6 referenced consistently.
