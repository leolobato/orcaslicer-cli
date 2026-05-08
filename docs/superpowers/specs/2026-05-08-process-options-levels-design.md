# Process Option Levels (Basic / Detailed / Advanced)

## Overview

Replace the binary allowlist concept (`app/process_allowlist.json`) with a three-tier level taxonomy so iOS / web clients can render progressive-disclosure pickers — **Basic** (small curated set), **Detailed** (most options), **Advanced** (everything).

Each option carries exactly one level. UIs filter cumulatively: selecting *Detailed* shows Basic + Detailed; selecting *Advanced* shows everything. The level lives next to each option in the API response so the UI can also render per-option badges without a second lookup.

The taxonomy starts from OrcaSlicer's upstream `ConfigOptionDef::mode` (`comSimple` / `comAdvanced` / `comDeveloper`) and is adjusted per-key via a small override JSON we control.

## Working principle alignment

GUI parity again drives the default. OrcaSlicer's GUI already shows options at three modes (Simple, Advanced, Developer) and tags every `ConfigOptionDef` with one. We harvest that signal at runtime so re-vendoring upstream automatically updates default level membership. Our override file expresses *intentional* deviations (e.g. promoting `wall_loops` from Detailed to Basic because we want it on the easy screen).

This matches the `CLAUDE.md` "Reuse over reimplement" rule: the upstream decision about each option's complexity flows through our wrapper unchanged unless we explicitly override it.

## Data flow

```
vendor/OrcaSlicer (C++)
       │
   ┌───┴───────────────────────────────┐
   ▼                                   ▼
orca-headless dump-options    scripts/extract_tab_layout.py
(adds `mode` per option,             │
 sourced from                        ▼
 ConfigOptionDef::mode)   cpp/src/generated/process_pages.json
       │                  (page → optgroup → keys)
       │                             │
       └─────────────┬───────────────┘
                     ▼
        app/options.py merge:
          1. Map upstream mode → level
             (comSimple → basic,
              comAdvanced → detailed,
              comDeveloper → advanced)
          2. Apply app/process_levels.json overrides
                     │
       ┌─────────────┴─────────────┐
       ▼                           ▼
GET /options/process        GET /options/process/layout
  metadata + level            pages → optgroups →
                              [{key, level}, ...]
```

## Level taxonomy

| Level      | Default source             | Intent                                                            |
|------------|----------------------------|-------------------------------------------------------------------|
| `basic`    | `comSimple` + overrides    | Small set; "I just want to print, don't show me knobs."           |
| `detailed` | `comAdvanced` + overrides  | Most knobs most users will reach for once comfortable.            |
| `advanced` | `comDeveloper` + overrides | Everything else, including engine-internal keys with no upstream mode tag. |

**Cumulative filter semantic.** `basic ⊆ detailed ⊆ advanced`. Clients filter "show options at level ≤ selected." A user who picks *Advanced* sees the whole catalogue; *Basic* shows only the smallest tier.

**Default for missing mode tag.** Some keys in `print_config_def.cpp` (e.g. internal slicer state that surfaces in 3MF round-trips) have no `mode` set. Treat as `advanced` so they remain reachable for power users via the modified view.

## Override file

`app/process_levels.json` (replaces the old `process_allowlist.json`):

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

Only listed keys deviate from upstream. Loader logs a warning for any override key not present in the metadata catalogue (typo / removed upstream / non-process key) — same pattern the old allowlist used.

## API shape

### `GET /options/process` — metadata catalogue (level added)

Per-option metadata gains a `level` field:

```json
{
  "version": "2.3.2-46",
  "levels_revision": "2026-05-08.1",
  "options": {
    "layer_height": {
      "label": "Layer height",
      "type": "float",
      "sidetext": "mm",
      "min": 0.0,
      "level": "basic"
    },
    "...": "..."
  }
}
```

Carrying `level` in the catalogue lets the modified-view rendering badge any key without cross-referencing the layout response — important for 3MF-customized keys that aren't in Tab.cpp's pages.

### `GET /options/process/layout` — paged editor layout

Each option becomes an object with `key` + `level` (was a bare string):

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

No server-side filtering. Empty optgroups and pages remain present (UIs may render them with placeholder copy when the user picks a level that hides everything in a group).

## Caching and versioning

Two cache keys, same as today:

- `version` — bumps when the binary changes (existing semantics: `{ORCA_VERSION}-{API_REVISION}`).
- `levels_revision` — bumps when `app/process_levels.json` changes. Replaces the old `allowlist_revision`.

Clients cache by both fields; bust on either change. The override JSON is intended to be edited frequently (curating the Basic/Detailed split is iterative product work) without rebuilding the binary.

## Implementation outline

Order of changes:

1. **`cpp/src/dump_options_mode.cpp`** — emit `mode` per option (string: `"simple"` / `"advanced"` / `"developer"` / `"none"`). Read from `ConfigOptionDef::mode`. Bumps `API_REVISION`.
2. **`app/process_levels.json`** — new file, seeded with the 12 keys from the existing allowlist, all stamped `basic`.
3. **`app/options.py`** — replace `_build_layout`'s allowlist filter with a level-stamping merge:
   - For each option in the catalogue: `level = override.get(key) or mode_to_level(metadata[key].mode) or "advanced"`.
   - Layout response: walk `process_pages.json`, stamp `{key, level}` per option.
   - Catalogue response: stamp `level` on each metadata entry.
4. **Delete `app/process_allowlist.json`** and **drop `PROCESS_ALLOWLIST_ENABLED`** from `app/config.py` and the in-flight working-copy changes (which are reverted as part of this work).
5. **Rename `scripts/check_allowlist.py` → `scripts/check_levels.py`** and **`tests/test_check_allowlist.py` → `tests/test_check_levels.py`**. Update both to read `process_levels.json`. The three drift checks (override key not in dump-options, override key not in `process_pages.json`, layout key not in dump-options) carry over unchanged — only the input filename + variable names need updating.
6. **`tests/test_options_module.py`** — replace allowlist-filter assertions with level-stamp assertions. New tests:
   - Catalogue response has `level` per option.
   - Layout option becomes `{key, level}`.
   - Override JSON wins over upstream mode.
   - Missing mode → `advanced`.
   - Unknown override key → startup warning logged.
7. **`docs/process-parameter-editor-api.md`** — rewrite the allowlist sections around levels. Drop the "permissive server" note from the standpoint of allowlist-as-UI-concept (still permissive, but the framing is now "level filters" not "allowlists").

## Migration

The earlier `feat/process-parameter-editor` working-copy changes that made the allowlist opt-in (`PROCESS_ALLOWLIST_ENABLED`) are dropped wholesale. The new spec lands on the pre-flag baseline:

```
git restore app/config.py app/main.py app/options.py tests/test_options_module.py
# then implement levels on this baseline
```

`app/process_allowlist.json` is removed. `scripts/check_allowlist.py` is renamed to `scripts/check_levels.py` and updated to read `process_levels.json` — its three drift checks (override key not in dump-options, override key not in `process_pages.json`, layout key not in dump-options) all transfer cleanly. `tests/test_check_allowlist.py` follows the rename.

iOS/web clients break on this change (their layout response shape changes from string → object) — explicitly accepted; the user is the only consumer and we ship coordinated.

## Out of scope

- Per-vendor or per-machine level overrides. Levels are global to all process options.
- Filament- and machine-domain levels. Same architecture extends when needed; not v1.
- Localising level labels server-side. Clients render their own "Basic" / "Detailed" / "Advanced" copy.
- Persisting the user's selected level. Client-side preference, no server contract.
- A "Beginner" / "Intermediate" / "Pro" relabeling. Levels are addressed by stable IDs (`basic` / `detailed` / `advanced`); display copy can change without an API break.
