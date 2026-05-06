# Process Parameter Editor (gateway / iOS / web sibling)

## Overview

Bring a GUI-parity process-parameter editor to the gateway-iOS and gateway-web sibling apps. Two views:

- **Main slicing screen — "Modified" view:** lists the parameters the project author customised away from the system process preset, mirroring OrcaSlicer's "Modified" indicator. Editable values flow back into the slice.
- **Editor screen — "All" view:** a paged list of process parameters, grouped and labelled exactly like the OrcaSlicer GUI (Quality / Strength / Speed / Support / Multimaterial / Others → optgroups → options).

Server-side curation gates which options are exposed via an allowlist (`app/process_allowlist.json`). The allowlist starts small and grows toward "everything" over time. Out-of-allowlist modifications on a project are still shown read-only in the Modified view.

Scope is **process-domain options only** for v1. Filament and machine editors are explicitly out of scope; the same architecture extends to them when needed.

## Working principle alignment

The core requirement is GUI parity. Every metadata field iOS/web consumes is sourced from libslic3r — `ConfigOptionDef` for per-option metadata, `Tab.cpp` for layout. We do not re-author labels, tooltips, types, min/max, enum values, or page/optgroup ordering. When OrcaSlicer is re-vendored, the extractors re-run and the editor follows upstream automatically.

This matches the `CLAUDE.md` "Reuse over reimplement" rule and the existing pattern set by `dump-profiles` (runtime metadata extraction from libslic3r).

## Data flow

```
vendor/OrcaSlicer (C++)
       │
   ┌───┴────────────────────────────┐
   ▼                                ▼
orca-headless dump-options    scripts/extract_tab_layout.py
(runtime, cpp/src/            (build-time, regex over Tab.cpp)
 dump_options_mode.cpp)              │
       │                             ▼
       │                  cpp/src/generated/process_pages.json
       │                  (checked in)
       │                             │
       └─────────────┬───────────────┘
                     ▼
       FastAPI (app/options.py)
                     ▲
                     │ HTTP
                     │
            ┌────────┴───────────────────┐
            │ gateway / iOS / web client │
            └────────────────────────────┘

Endpoints exposed:
  GET  /options/process                  ← unfiltered metadata
  GET  /options/process/layout           ← allowlist-filtered page→optgroup
  GET  /3mf/{token}/inspect              ← extended with process_modifications
  POST /slice/v2 + process_overrides     ← new form field
```

Two extractors (one runtime, one build-time), three new/extended endpoints, one new slice form field.

## C++ extractors

### `orca-headless dump-options` (runtime)

New file `cpp/src/dump_options_mode.cpp`, registered in `cpp/src/orca_headless.cpp`. Mirrors the structure of the existing `dump_profiles_mode.cpp`.

Behaviour:
1. Initialises `Slic3r::print_config_def` (no preset bundle, no resources directory required — pure metadata).
2. Iterates `print_config_def.options` (a `std::map<t_config_option_key, ConfigOptionDef>`).
3. Filters to process-domain options. Concrete filter: option must have a non-empty `category`, must not be a filament-only or machine-only option (we exclude options whose `category` is "Filament" or whose key matches the existing `filament_*` / `*_filament` / `printer_*` exclusion sets used in `app/slicer.py`).
4. Emits one JSON record per option with:

```json
{
  "key": "layer_height",
  "label": "Layer height",
  "category": "Quality",
  "tooltip": "...",
  "type": "coFloat",
  "sidetext": "mm",
  "default": "0.2",
  "min": 0.0,
  "max": 0.6,
  "enum_values": null,
  "enum_labels": null,
  "mode": "simple",
  "gui_type": null,
  "nullable": false,
  "readonly": false
}
```

Each field maps directly to a `ConfigOptionDef` member. `min`/`max` are omitted when they hold `±FLT_MAX` sentinels. `default` is serialised through libslic3r's existing string serialisers (same path the GUI uses to render the field's initial value), so vector and percent options come out in the canonical config string form. `type` is the stringified `ConfigOptionType` enum (e.g. `coFloat`, `coInt`, `coBool`, `coString`, `coEnum`, `coPercent`, `coFloatOrPercent`, `coPoints`, …).

Output protocol matches the existing headless modes: a sentinel-framed JSON document on stdout, returned to Python via `BinaryClient.dump_options()`.

### `scripts/extract_tab_layout.py` (build-time)

`Tab.cpp::TabPrint::build()` is wxWidgets-coupled; we don't link it. Instead we parse it with regex.

Three call shapes to match:

```cpp
auto page = add_options_page(L("Quality"), "empty");
auto optgroup = page->new_optgroup(L("Layer height"));
optgroup->append_single_option_line("layer_height","quality_settings_layer_height");
```

Regexes:
- `add_options_page\(\s*L\("([^"]+)"\)` → page label
- `new_optgroup\(\s*L\("([^"]+)"\)` → optgroup label
- `append_single_option_line\(\s*"([^"]+)"` → option key

The script walks `vendor/OrcaSlicer/src/slic3r/GUI/Tab.cpp` line by line, scoped to the `TabPrint::build()` function body (delimited by the function signature and the matching closing brace at column 0). Maintains a "current page" and "current optgroup" pointer as it sees each call.

Output: `cpp/src/generated/process_pages.json`, **checked into git**:

```json
{
  "extracted_from_sha": "abc123...",
  "extracted_from_path": "vendor/OrcaSlicer/src/slic3r/GUI/Tab.cpp",
  "extracted_at": "2026-05-06",
  "pages": [
    {
      "label": "Quality",
      "optgroups": [
        {"label": "Layer height", "options": ["layer_height", "initial_layer_print_height"]},
        {"label": "Line width", "options": ["line_width", "initial_layer_line_width"]}
      ]
    }
  ]
}
```

`extracted_from_sha` is the git blob SHA of `Tab.cpp` at extraction time. The script's `--check` mode compares against the current SHA and exits non-zero if they differ — this gates CI on PRs that re-vendor OrcaSlicer.

`make extract-layout` (or `python scripts/extract_tab_layout.py`) is invoked manually after each vendor bump. We accept the regex's brittleness because the call shape has been stable across OrcaSlicer versions for years; if upstream restructures `Tab.cpp` we upgrade to libclang.

## Python endpoints

New module `app/options.py` owns option-metadata loading. Initialised in `lifespan` after the existing profile load. Refreshed on `POST /profiles/reload`.

### `GET /options/process` — full metadata catalogue

```json
{
  "version": "2.3.2-37",
  "options": {
    "layer_height": { ...per-option metadata as in the dump-options shape... },
    "wall_loops": { ... }
  }
}
```

Keyed by option name. **Unfiltered** — every process-domain option emitted by `dump-options` is present, regardless of allowlist. iOS uses this as the metadata reference for any key it needs to render, including out-of-allowlist keys shown read-only in the Modified view.

### `GET /options/process/layout` — allowlist-filtered layout

```json
{
  "version": "2.3.2-37",
  "allowlist_revision": "2026-05-06.1",
  "pages": [
    {
      "label": "Quality",
      "optgroups": [
        {"label": "Layer height", "options": ["layer_height", "initial_layer_print_height"]}
      ]
    },
    {
      "label": "Strength",
      "optgroups": [
        {"label": "Walls", "options": ["wall_loops", "top_shell_layers", "bottom_shell_layers"]}
      ]
    }
  ]
}
```

Built by:
1. Loading `cpp/src/generated/process_pages.json`.
2. Loading `app/process_allowlist.json`.
3. Filtering: drop options not in the allowlist set; drop optgroups whose `options` list is empty after filtering; drop pages whose `optgroups` list is empty.
4. Stamping `version` (from API config) and `allowlist_revision` (from the allowlist file).

Powers the iOS/web "All" view directly. Within each surviving optgroup the option ordering is preserved exactly as it appears in `Tab.cpp`.

### `GET /3mf/{token}/inspect` — extended

Adds one new top-level field `process_modifications`:

```json
{
  "...existing fields...": "...",
  "process_modifications": {
    "process_setting_id": "GP004",
    "modified_keys": ["layer_height", "wall_loops", "some_obscure_key"],
    "values": {
      "layer_height": "0.16",
      "wall_loops": "3",
      "some_obscure_key": "true"
    }
  }
}
```

`process_setting_id` is read from `project_settings.config`. `modified_keys` is `different_settings_to_system[0]` from the same file, **unfiltered** — out-of-allowlist keys are included so iOS can render them read-only. `values` is the resolved value for each modified key from `project_settings.config`.

Cached in the existing `InspectCache` alongside the rest of the inspect payload — no extra parse cost on subsequent calls.

### Existing endpoints

`GET /profiles/processes/{setting_id}` is unchanged for this feature. iOS uses it to fetch the system-default anchor for the diff. (The separate web-ui design adds an `inheritance_chain` field to it; orthogonal.)

## Slice override path

### New form field on `/slice/v2` and `/slice-stream/v2`

```
process_overrides: '{"layer_height": "0.16", "wall_loops": "3"}'
```

Optional. JSON object, **string values** (matches OrcaSlicer's config convention — every value in `project_settings.config` is stringified, including booleans, ints, and vectors). Absent or empty `{}` behaves exactly like today.

### Overlay order

`app/slicer.py` already overlays the 3MF customisations on top of the resolved system process profile. The new field adds one more overlay **after** the 3MF overlay so that client edits win:

```
resolved system process profile
   └── overlay 3MF customizations (different_settings_to_system[0],
   │                               filament_* keys excluded)
   │
   └── overlay process_overrides   ◀── new
   │
   └── written as temp JSON, fed to orca-slicer CLI
```

Rationale: the iOS user sees `layer_height=0.16` in the Modified view (sourced from the 3MF), edits it to `0.20`, slices. The user expects `0.20`. Client overrides are the highest-priority layer.

The existing filament-key exclusion and `_CLAMP_RULES` validation apply identically to the new layer — same code path, one more dict merged in.

### Validation

- **Server-side allowlist enforcement: NO.** Per Q5b/Q6, the allowlist is a UI concept. The server accepts any valid process-domain key in `process_overrides`.
- **Server-side range/type validation: minimal.** Per Q5a, validation is "match the GUI" — and the GUI does field-level clamping with `min/max/enum_values` from `ConfigOptionDef`. iOS replicates that clamp using metadata from `/options/process` before submitting. The server still runs `_CLAMP_RULES` as a final safety net (same as for 3MF customisations today).
- **Cross-field validation: explicitly out of scope for v1.** GUI cross-field rules (e.g. layer_height ≤ 0.75 × nozzle_diameter, support fields disabled when `enable_support=false`) live as scattered C++ callbacks in `Tab.cpp`. Replicating them is a much larger surface and not required for the editor to be useful. OrcaSlicer's slice-time validation still catches the worst combinations.

### Response surface

Add one new response header:

- **`X-Process-Overrides-Applied`** — JSON array of `{key, value, previous}` where `previous` is the value before the override (i.e. the 3MF-customised value if the 3MF touched the key, otherwise the resolved system default).

Existing headers (`X-Settings-Transfer-Status`, `X-Settings-Transferred`, `X-Filament-Settings-Transferred`, `X-Machine-Settings-Transferred`) are unchanged. This keeps `X-Settings-Transferred` strictly about 3MF-sourced transfers and gives the client overrides their own dedicated channel.

Same-key collisions between 3MF customisation and client override produce two entries — one in `X-Settings-Transferred` (the 3MF transfer ran) and one in `X-Process-Overrides-Applied` with `previous` set to the 3MF value. The reader can reconstruct the full chain.

## Allowlist storage and curation

### File: `app/process_allowlist.json`

```json
{
  "revision": "2026-05-06.1",
  "options": [
    "layer_height",
    "initial_layer_print_height",
    "wall_loops",
    "top_shell_layers",
    "bottom_shell_layers",
    "sparse_infill_density",
    "sparse_infill_pattern",
    "enable_support",
    "support_type",
    "support_threshold_angle",
    "brim_type",
    "brim_width"
  ]
}
```

Initial seed is ~12 of the most user-facing knobs. We tune by what users actually want to edit. `revision` is a free-form string (calver-ish); bump whenever the list changes. Surfaced in `/options/process/layout` so clients can cache by revision.

### Curation flow

To widen the allowlist:
1. Edit `app/process_allowlist.json`, add the keys.
2. Bump `revision`.
3. Run `scripts/check_allowlist.py` locally (described below).
4. Commit. iOS and web pick up the change on their next call to `/options/process/layout` — no client release needed.

### Drift checks: `scripts/check_allowlist.py`

Inputs: `app/process_allowlist.json`, `cpp/src/generated/process_pages.json`, and a fresh `orca-headless dump-options` invocation (the script shells out to the built binary; CI already produces it). Catches three drift modes:

1. **Allowlist key not in `dump-options`.** Typo, removed upstream, or filament/machine-domain leak. Loader logs a warning and skips. CI lint fails the PR.
2. **Allowlist key in `dump-options` but not in `process_pages.json`.** Key exists in libslic3r but isn't surfaced in the GUI's process Tab — nowhere to render it. Loader skips. CI lint fails.
3. **`process_pages.json` references a key not in `dump-options`.** Tab.cpp regex caught a stale reference, or upstream renamed a key without updating the Tab. CI lint fails; triage during the next vendor bump.

The script runs in CI on every PR.

### Out of scope for the allowlist

- **Filament-domain options.** Sibling `filament_allowlist.json` when we get there.
- **Machine-domain options.** Same.
- **Cross-field visibility/enable rules.** Allowlist is purely "is this key editable". Conditional rules are phase-2.

## File and module changes

| Path | Status | Purpose |
|---|---|---|
| `cpp/src/dump_options_mode.{h,cpp}` | new | runtime metadata dump from `print_config_def` |
| `cpp/src/orca_headless.cpp` | modified | register `dump-options` subcommand |
| `scripts/extract_tab_layout.py` | new | build-time Tab.cpp → JSON layout extractor |
| `cpp/src/generated/process_pages.json` | new (generated, checked in) | extracted page→optgroup→option layout |
| `scripts/check_allowlist.py` | new | drift checks, run in CI |
| `app/process_allowlist.json` | new | curated set of editable process keys |
| `app/options.py` | new | metadata + layout loader, served by new endpoints |
| `app/main.py` | modified | mount `/options/process`, `/options/process/layout`; extend `/3mf/{token}/inspect`; accept `process_overrides` form field on slice endpoints |
| `app/slicer.py` | modified | apply `process_overrides` overlay after 3MF transfer; emit `X-Process-Overrides-Applied` header |
| `app/binary_client.py` | modified | add `BinaryClient.dump_options()` shelling out to `orca-headless dump-options` |

## Versioning

The existing API version `{ORCA_VERSION}-{API_REVISION}` covers the metadata catalogue: re-vendoring OrcaSlicer bumps `ORCA_VERSION`, schema changes to the new endpoints bump `API_REVISION`. Clients invalidate caches on `version` change.

The allowlist has its own `allowlist_revision` so iOS/web can cache `/options/process/layout` independently of the API version — widening the allowlist doesn't require an OrcaSlicer rebuild.

## Test plan

- **Unit:** `tests/test_options_dump_parsing.py` — given a synthetic `dump-options` JSON, verify `app/options.py` parses each `ConfigOptionType` correctly (especially `coEnum`, `coPercent`, `coFloatOrPercent`, vector types).
- **Unit:** `tests/test_layout_filter.py` — given a fixture `process_pages.json` and an allowlist, verify the filter drops empty optgroups and pages, preserves option ordering, and stamps `allowlist_revision`.
- **Unit:** `tests/test_extract_tab_layout.py` — feed the extractor a synthetic `Tab.cpp` snippet and verify the three regex shapes match correctly, including nested calls and comment-stripping.
- **Unit:** `tests/test_check_allowlist.py` — verify each of the three drift modes is detected.
- **Integration:** `tests/integration/test_process_overrides.py` — slice fixture 01 with `process_overrides={"layer_height": "0.16"}`, verify the resulting gcode reports `; layer_height = 0.16` (or equivalent), verify `X-Process-Overrides-Applied` header content, verify same-key collision against a 3MF that also customises `layer_height`.
- **Integration:** `tests/integration/test_inspect_process_modifications.py` — POST a 3MF with `different_settings_to_system[0] = ["layer_height", "wall_loops"]`, verify `/3mf/{token}/inspect` returns `process_modifications.modified_keys` and `values` correctly.
- **CI:** `scripts/check_allowlist.py --check` runs on every PR.

## Phasing

This spec is one implementation plan. Suggested execution order:

1. **C++ side first**: implement `dump_options_mode`, run it, commit a snapshot of its output as a fixture for Python unit tests.
2. **Build-time extractor**: write `extract_tab_layout.py`, commit the generated `process_pages.json`.
3. **Python options module**: load both, expose `/options/process` and `/options/process/layout`. Unit tests at this point.
4. **Inspect extension**: add `process_modifications` to `/3mf/{token}/inspect`. Tests.
5. **Slice overrides**: add the form field, the overlay layer in `slicer.py`, and the response header. Integration test.
6. **Drift checks**: `scripts/check_allowlist.py` + CI wiring.

Each step is independently shippable — clients can start consuming `/options/process` and `/options/process/layout` before the slice overrides land.

## Out of scope (and why)

- **Filament and machine editors.** Q1 scoped to process-only. Same architecture extends; revisit when needed.
- **Cross-field validation rules.** Would require porting `Tab.cpp` change-handlers; large surface, not required for the editor to be useful. OrcaSlicer's existing slice-time validation backstops.
- **Persisting user edits.** Per the brainstorm: edits are per-slice. The gateway/iOS may persist them in their own store; the slicer API is stateless w.r.t. user customisations.
- **Conditional show/hide of options.** GUI hides support-related options when `enable_support=false`; we do not. iOS/web can implement this client-side using `/options/process` metadata if desired.
- **Web UI implementation.** This spec defines the API; the web UI consuming it is a separate frontend task (possibly built on the existing `docs/superpowers/specs/2026-04-07-web-ui-design.md` foundation).
