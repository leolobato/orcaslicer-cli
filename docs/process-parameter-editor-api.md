# Process Parameter Editor — API guide for gateway/iOS/web

This document describes the new process-parameter editor surface introduced in `feat/process-parameter-editor`. The gateway, iOS, and web sibling apps consume these endpoints to render a GUI-parity process-parameter editor.

The feature is **process-domain only** for v1. Filament and machine editors are out of scope (sibling allowlists when needed).

Design spec: [`docs/superpowers/specs/2026-05-06-process-parameter-editor-design.md`](superpowers/specs/2026-05-06-process-parameter-editor-design.md). API revision bumped to **41** (full version string `2.3.2-41`).

---

## At a glance

Two UI views in the consuming apps:

- **Modified view** (main slicing screen): shows parameters the project author customised away from the system process preset. Editable when the key is in the server-side allowlist; read-only otherwise. Editable values flow back into the slice as `process_overrides`.
- **All view** (editor screen): paged list of all process parameters that the server has decided to expose, grouped exactly like OrcaSlicer's GUI Tab (Quality / Strength / Speed / Support / Multimaterial / Others → optgroups → options).

Three new endpoints, one extended endpoint, one new field on the slice request.

| Endpoint | Method | Purpose |
|---|---|---|
| `/options/process` | GET | Per-option metadata catalogue. Unfiltered. |
| `/options/process/layout` | GET | Page → optgroup → option layout, allowlist-filtered. |
| `/3mf/{token}/inspect` | GET (extended) | Adds `process_modifications` block. |
| `/slice/v2`, `/slice-stream/v2` | POST (new field) | Accept `process_overrides`. Response carries `process_overrides_applied`. |

---

## `GET /options/process` — option metadata catalogue

Returns metadata for every process-domain option known to libslic3r's `print_config_def`. **Unfiltered** — every option the C++ side recognises is here, including ones not in the allowlist. iOS/web uses this to render labels, tooltips, types, min/max, enums for any key it needs to display, including out-of-allowlist keys shown read-only in the Modified view.

Response shape:

```json
{
  "version": "2.3.2-41",
  "options": {
    "layer_height": {
      "key": "layer_height",
      "label": "Layer height",
      "category": "Quality",
      "tooltip": "Slicing height for every layer...",
      "type": "coFloat",
      "sidetext": "mm",
      "default": "0.2",
      "min": 0.0,
      "max": null,
      "enum_values": null,
      "enum_labels": null,
      "mode": "simple",
      "gui_type": "",
      "nullable": false,
      "readonly": false
    },
    "wall_loops": { ... },
    "seam_position": {
      "key": "seam_position",
      "label": "Seam position",
      "category": "Quality",
      "tooltip": "...",
      "type": "coEnum",
      "sidetext": "",
      "default": "aligned",
      "min": null,
      "max": null,
      "enum_values": ["nearest", "aligned", "back", "random"],
      "enum_labels": ["Nearest", "Aligned", "Back", "Random"],
      "mode": "simple",
      "gui_type": "",
      "nullable": false,
      "readonly": false
    }
  }
}
```

Keyed by `key` for cheap O(1) client-side lookup. Currently ~609 entries.

### Per-option fields

| Field | Type | Notes |
|---|---|---|
| `key` | string | Stable identifier. Same as the JSON key. |
| `label` | string | Human label. Already localised by libslic3r's `_L()` macro. |
| `category` | string | GUI tab name: `Quality`, `Strength`, `Speed`, `Support`, `Advanced`, `Others`, `Flush options`, `Extruders`, `Layers and Perimeters`, `Other`. Empty string for options the C++ registry didn't categorise. |
| `tooltip` | string | Hover/help text. Localised. |
| `type` | string | One of `coBool`, `coFloat`, `coFloats`, `coInt`, `coInts`, `coString`, `coStrings`, `coPercent`, `coPercents`, `coFloatOrPercent`, `coFloatsOrPercents`, `coPoint`, `coPoints`, `coPoint3`, `coBools`, `coEnum`, `coNone`. Drives input-widget choice. |
| `sidetext` | string | Unit suffix (`mm`, `mm/s`, `%`, etc.). Display next to the input. |
| `default` | string | System default, serialised through libslic3r's own writer. Vector / percent / enum encoding round-trips through this same writer. |
| `min`, `max` | number \| null | Range bounds. `null` when the option has no bound. |
| `enum_values` | array of strings \| null | Enum option keys. Pair index-wise with `enum_labels` for display. |
| `enum_labels` | array of strings \| null | Display labels for enum options. May be empty/null even when `enum_values` exists; in that case display `enum_values` directly. |
| `mode` | string | `simple` / `advanced` / `develop`. The OrcaSlicer GUI hides advanced/develop based on the user's mode preference. iOS/web can choose to honour or ignore this. |
| `gui_type` | string | `""`, `i_enum_open`, `f_enum_open`, `color`, `select_open`, `slider`, `legend`, `one_string`. Hint about widget kind (e.g., `color` → colour picker, `slider` → slider). |
| `nullable` | boolean | Vector option allows `nil` entries. |
| `readonly` | boolean | Setting is computed/derived; UI should not edit. |

### Caching

Cache aggressively client-side. Bust on `version` change.

---

## `GET /options/process/layout` — paged editor layout

Returns the page → optgroup → option layout, **filtered server-side by `app/process_allowlist.json`**. Empty optgroups and pages are dropped. Powers the "All" view directly.

Response shape:

```json
{
  "version": "2.3.2-41",
  "allowlist_revision": "2026-05-06.1",
  "pages": [
    {
      "label": "Quality",
      "optgroups": [
        {
          "label": "Layer height",
          "options": ["layer_height", "initial_layer_print_height"]
        }
      ]
    },
    {
      "label": "Strength",
      "optgroups": [
        {
          "label": "Walls",
          "options": ["wall_loops", "top_shell_layers", "bottom_shell_layers"]
        },
        {
          "label": "Infill",
          "options": ["sparse_infill_density", "sparse_infill_pattern"]
        }
      ]
    },
    {
      "label": "Support",
      "optgroups": [
        {
          "label": "Support",
          "options": ["enable_support", "support_type", "support_threshold_angle"]
        }
      ]
    },
    {
      "label": "Others",
      "optgroups": [
        {
          "label": "Skirt and brim",
          "options": ["brim_type", "brim_width"]
        }
      ]
    }
  ]
}
```

Page ordering matches the GUI Tab. Optgroup ordering within each page matches the GUI optgroup order. Option ordering within each optgroup matches the GUI's `append_single_option_line` sequence (i.e., the visual order in the Tab). All ordering is harvested from `Tab.cpp::TabPrint::build()` at build time.

### Caching

Two cache keys: `version` and `allowlist_revision`. The allowlist can grow without an API rebuild — `version` stays stable but `allowlist_revision` bumps. Cache by both fields; bust on either change.

### Allowlist semantics

- **Allowlisted options** are editable in the iOS/web UI.
- **Non-allowlisted options** never appear in `/options/process/layout`'s pages (filtered server-side).
- The allowlist starts small (~12 keys at v1) and grows toward "everything" over time.
- Server is permissive on slice — see `process_overrides` below — so iOS/web can technically submit any option key. The allowlist is a UI concept, not an enforcement concept.

---

## `GET /3mf/{token}/inspect` — extended

The existing inspect endpoint gains one new top-level field, `process_modifications`. The rest of the response is unchanged.

```json
{
  "schema_version": 4,
  "is_sliced": false,
  "plate_count": 1,
  "plates": [...],
  "filaments": [...],
  "estimate": null,
  "bbox": {...},
  "printer_model": "Bambu Lab A1 mini",
  "printer_variant": "0.4",
  "curr_bed_type": "Textured PEI Plate",
  "printer_settings_id": "Bambu Lab A1 mini 0.4 nozzle",
  "print_settings_id": "Custom 0.20mm Standard",
  "layer_height": "0.16",

  "process_modifications": {
    "process_setting_id": "Custom 0.20mm Standard",
    "modified_keys": ["layer_height", "wall_loops", "some_obscure_key"],
    "values": {
      "layer_height": "0.16",
      "wall_loops": "3",
      "some_obscure_key": "true"
    }
  }
}
```

`schema_version` bumped from 3 to 4. Existing callers continue to work; they just see one extra top-level key.

### Field semantics

- `process_setting_id`: the project's authored process preset name (from `print_settings_id` in the 3MF's `project_settings.config`).
- `modified_keys`: the contents of `different_settings_to_system[0]` from the project settings — the project author's own fingerprint of which process keys they customised away from the system preset. **Unfiltered** — out-of-allowlist keys are included so iOS/web can show them read-only.
- `values`: each modified key's value, read directly from `project_settings.config` and stringified.

### Modified-view rendering recipe

For each `(key, value)` in `process_modifications.values`:

1. Look up the key's metadata from `/options/process` (label, type, sidetext, enum labels, min/max).
2. Look up the key in `/options/process/layout`'s flattened option set.
3. If the key is allowlisted (present in step 2): render an editable widget. Initial value is `value`. On change, add to the `process_overrides` payload sent at slice time.
4. If not allowlisted: render the same widget read-only with the value displayed. No edit.

For the "system default" comparison anchor, fetch the resolved process profile via `GET /profiles/processes/{process_setting_id}` (existing endpoint). Each modified key's "before" is the resolved profile's value for that key.

### Empty/missing cases

- 3MF without `project_settings.config`: `process_modifications = {"process_setting_id": "", "modified_keys": [], "values": {}}`.
- 3MF with `project_settings.config` but no `different_settings_to_system`: `modified_keys = []`, `values = {}`. `process_setting_id` is still surfaced.
- Empty `different_settings_to_system[0]`: same as above — the project author didn't customise anything process-side.

---

## `process_overrides` on `/slice/v2` and `/slice-stream/v2`

`SliceTokenRequest` gains one optional field:

```python
process_overrides: dict[str, str] | None = None
```

Example slice request body:

```json
{
  "input_token": "...",
  "machine_id": "GM004",
  "process_id": "GP004",
  "filament_settings_ids": ["GFA00"],
  "process_overrides": {
    "layer_height": "0.16",
    "wall_loops": "3"
  }
}
```

Field semantics:

- **String values only.** Match OrcaSlicer's config-string convention (every value in `project_settings.config` is stringified, including booleans, ints, and vectors). For booleans use `"1"` / `"0"`. For percents use `"50%"`. For floats use `"0.16"`. For enums use the enum key string (e.g., `"aligned"`).
- **Permissive server.** Any process-domain key is accepted. The server does not enforce the allowlist on submission — that's a UI concept.
- **Filament-domain keys are dropped.** Keys starting with `filament_` or ending with `_filament` are silently skipped on the server (they belong to the filament editor).
- **Unknown keys are silently dropped.** No error if the key doesn't exist in libslic3r.
- **Bad values are silently dropped.** A value libslic3r can't parse for the option's type (e.g. `"abc"` for `coFloat`) is skipped; the option keeps its previous value.
- **Absent / `null` / `{}` is a no-op.** Existing callers that don't pass `process_overrides` see no behaviour change.

### Overlay precedence

The server resolves the process config in this order:

```
system process profile (resolved from process_id)
   └── overlay 3MF customisations (different_settings_to_system[0])
          └── overlay process_overrides   ◀── client wins
                 └── final config used for slicing
```

Client overrides **win** over both the system preset and the 3MF's own customisations. Rationale: the user opened the 3MF, saw `layer_height=0.16` (from the 3MF), edited it to `0.20`, and sliced — they expect `0.20`.

### Response feedback

The slice response's `settings_transfer` dict gains one new key, `process_overrides_applied`:

```json
{
  "input_token": "...",
  "output_token": "...",
  "estimate": {...},
  "settings_transfer": {
    "status": "applied",
    "process_keys": ["layer_height"],
    "printer_keys": [],
    "filament_slots": [...],
    "curr_bed_type": "Textured PEI Plate",
    "process_overrides_applied": [
      {
        "key": "layer_height",
        "value": "0.16",
        "previous": "0.20"
      },
      {
        "key": "wall_loops",
        "value": "3",
        "previous": "2"
      }
    ]
  },
  "thumbnail_urls": [],
  "download_url": "/3mf/.../"
}
```

Each `process_overrides_applied` entry:

- `key`: the option key the client submitted.
- `value`: the stringified value the client submitted.
- `previous`: the value the option held *before* the client override took effect. If the 3MF customised this key, `previous` is the 3MF-customised value; otherwise it's the resolved system default. Always different from `value` (otherwise the client wouldn't have submitted the override).

The list is empty (`[]`) when no `process_overrides` were submitted or when none could be applied (all dropped as filament-domain / unknown / unparseable).

`settings_transfer.status` is `"applied"` whenever **any** layer transferred something — including client-only overrides on a clean 3MF. (Before this feature, `status` only reflected 3MF-sourced transfers.)

### Same-key collisions

If the 3MF customised `layer_height=0.18` AND the client submits `process_overrides={"layer_height": "0.16"}`:

- The final config has `layer_height=0.16` (client wins).
- `settings_transfer.process_keys` includes `"layer_height"` (the 3MF transfer ran).
- `settings_transfer.process_overrides_applied` includes `{"key": "layer_height", "value": "0.16", "previous": "0.18"}`.
- The reader can reconstruct the full chain: system default → 0.18 (3MF) → 0.16 (client).

---

## End-to-end workflow

1. **App launch** (cold or after invalidation):
   - `GET /options/process` → cache catalogue keyed by `version`.
   - `GET /options/process/layout` → cache layout keyed by `(version, allowlist_revision)`.

2. **User opens a project** (3MF):
   - Existing: `POST /3mf` to upload, get token; `GET /3mf/{token}/inspect` for project metadata.
   - **New:** consume `process_modifications` from the inspect response.
   - For the Modified-view diff, resolve the system preset values: `GET /profiles/processes/{process_setting_id}`.

3. **Render Modified view**:
   - For each `key` in `process_modifications.modified_keys`:
     - Metadata from `/options/process`.
     - Allowlisted (in `/options/process/layout`)? Render editable. Otherwise read-only.
     - Show the project's value (from `process_modifications.values[key]`) and the system default for context.

4. **User taps "All settings"** → render `/options/process/layout` page-by-page. For each leaf option, look up metadata from `/options/process`. The "current" value is either the user's local edit or, falling back, the value resolved from the process preset (treat un-edited options as "default").

5. **User edits values**: track only the keys the user actually changes (don't submit untouched options). Build a `dict[str, str]` of stringified values.

6. **User taps Slice**: include the override map in the slice request:

   ```json
   {
     "input_token": "...",
     "machine_id": "...",
     "process_id": "...",
     "filament_settings_ids": [...],
     "process_overrides": { ... user's edits ... }
   }
   ```

7. **Slice response**: read `settings_transfer.process_overrides_applied` to confirm which overrides took effect. Surface to the user (e.g., "2 settings applied"). Keys that were dropped (filament-domain, unknown, unparseable) won't appear — surface a warning if needed.

---

## Type → input widget mapping (suggested)

| `type` | Widget |
|---|---|
| `coBool` | toggle / switch (value `"1"` / `"0"`) |
| `coInt`, `coInts` | numeric stepper (clamped to `min`/`max` if set) |
| `coFloat`, `coFloats` | numeric input with decimal (clamped) |
| `coPercent`, `coPercents` | numeric input with `%` suffix; submit value as `"50%"` |
| `coFloatOrPercent`, `coFloatsOrPercents` | numeric input + unit toggle (mm vs %) |
| `coString`, `coStrings` | text input |
| `coEnum` | dropdown using `enum_values` + `enum_labels` |
| `coPoint`, `coPoints`, `coPoint3` | coordinate inputs |
| `gui_type=color` | colour picker |
| `gui_type=slider` | slider (use `min`/`max`) |
| `gui_type=one_string` | text input even when `type` is a vector |

Use the option's `min`/`max` for client-side clamping. Use `nullable: true` to permit blank entries on vector options.

---

## Versioning and cache invalidation

- `version` (in both `/options/process` and `/options/process/layout`) follows the API revision: `{ORCA_VERSION}-{API_REVISION}`. Currently `2.3.2-41`. Bumps when libslic3r is re-vendored or the API schema changes.
- `allowlist_revision` (in `/options/process/layout` only) is calver-ish (`2026-05-06.1`). Bumps when the curated allowlist grows or shrinks. Allows the editor to gain or lose options without an API rebuild.

Recommended client cache keys:

- `/options/process` → `version`. ~609 entries; expect this payload to be ~150 KB. Cache for the lifetime of the API connection.
- `/options/process/layout` → `(version, allowlist_revision)`. Smaller (only allowlisted keys); refresh whenever revision changes.
- `/3mf/{token}/inspect` → server-side cached by sha256; client may cache by token.
- `/profiles/processes/{setting_id}` → cache by `(version, setting_id)`.

If the API returns 503 with `code=options_not_loaded` or `options_layout_not_loaded`, the server's options cache failed to populate at startup. Retry after a brief delay; if it persists, escalate to the API operator.

---

## Limitations and non-goals (v1)

- **Process-only.** Filament and machine editors are out of scope.
- **No cross-field validation.** The GUI hides `support_threshold_angle` when `enable_support=false` and clamps `layer_height ≤ 0.75 × nozzle_diameter`. We expose the per-option metadata but **do not** mirror these conditional rules. Implement client-side if needed; OrcaSlicer's slice-time validators backstop bad combinations.
- **No persistence.** `process_overrides` is per-slice. The gateway/iOS/web may persist user edits in their own store; the slicer API is stateless.
- **Allowlist is curated.** Even though `/options/process` returns ~609 entries, only ~12 are editable in v1 (`/options/process/layout`). The allowlist will grow.
