# Export user filaments for OrcaSlicer GUI import

## Goal

Let users download their user-imported filament profiles as standalone JSON
files that can be imported into OrcaSlicer's GUI. The default export shape
("flattened") is self-contained and AMS-assignable on import. A second shape
("thin") preserves `inherits` for installs that already have the parent
profile.

## Motivation

User filaments are now stored raw on disk with `inherits` preserved (see
`docs/superpowers/specs/2026-04-29-defer-user-profile-flattening-design.md`).
The slice path resolves the chain on demand. There is currently no path for
the user to take their custom filament back out of the API and into the
OrcaSlicer GUI — for sharing, for backup, or for AMS spool assignment in the
GUI.

The OrcaSlicer GUI requires a profile to have `instantiation: "true"`, a
non-empty `setting_id`, and a non-empty `filament_id` to make it
AMS-assignable (matching the same rules CLAUDE.md documents for our
listing). All three are present on the resolved (flattened) view but only
`setting_id` and `instantiation` are guaranteed on the raw saved file —
`filament_id` may live on the parent. So a thin export imports successfully
but cannot be AMS-assigned; a flattened export can.

## Scope

- Add `GET /profiles/filaments/{setting_id}/export` (single).
- Add `POST /profiles/filaments/export-batch` (zip).
- Both endpoints accept a `shape` selector: `flattened` (default) or `thin`.
- Both endpoints only export **user** filaments (loaded from
  `USER_PROFILES_DIR`). Bundled vendor filaments are not exportable.
- Add an "Export…" button + modal to the embedded web UI.

Out of scope:

- Process exports. Filaments only — that is what the user asked for. If
  process export becomes interesting later, the same pattern lifts.
- Machine exports. Not applicable (no user import path).
- Migrating any existing on-disk format.
- Server-side persistence of export choices.

## Behavior

### Export shapes

`flattened` (default):

The flatten algorithm targets the OrcaSlicer GUI's user-filament `base/`
shape. The shape was reverse-engineered from the GUI source
(`AMSMaterialsSetting.cpp:872-915`, `Preset.cpp:1209-1362`) — see
"Flatten algorithm derivation" below for the source-trace.

For each `(user_filament, target_printer)` pair, produce one JSON file:

1. **Resolve the chain** via `get_profile("filament", setting_id)` — same
   path the slicer uses.
2. **Strip vendor-profile markers and parent refs**: pop `type`,
   `instantiation`, `setting_id`, `inherits`, `base_id`.
3. **Set `inherits: ""`** (empty string, present). The GUI's
   `is_base_preset` predicate (`Preset.hpp:738`) and
   `set_custom_preset_alias` (`Preset.cpp:3262-3281`) require the field
   to be present-and-empty for user-root presets.
4. **Rewrite `name` and `filament_settings_id`** to embed the target
   printer:
   - `name = "<alias> @<target_printer>"` where `<alias>` is the user
     filament's original name (without `@…`).
   - `filament_settings_id = ["<alias> @<target_printer>"]` (length-1
     list, matches name).
   The `@<printer>` suffix is **mandatory**: the AMS dropdown filter
   at `AMSMaterialsSetting.cpp:900-914` parses the alias as
   `name.substr(0, name.find('@')-1)` and silently drops any user
   preset whose name lacks `@`.
5. **Scope `compatible_printers`** to `[<target_printer>]`. Required by
   `AMSMaterialsSetting.cpp:882-884`.
6. **Pad per-variant list keys** to the target printer's
   `extruder_variant` count. Replicate the slot-0 value across all
   variants. Length-1 against length-N consumers is the most likely
   crash trigger we hit during empirical testing. The set of
   per-variant keys is derived dynamically from the resolved profile:
   keys whose value is a list of length 1 in the resolved chain and
   whose canonical length comes from the machine profile's
   `filament_extruder_variant` count.
7. **Add empty-default GUI compatibility fields** if missing:
   - `compatible_printers_condition: ""`
   - `compatible_prints: []`
   - `compatible_prints_condition: ""`
8. **Preserve** `from` (typically `"User"`), `name` (after rewrite),
   `filament_id` (must be non-empty), `version` (must parse as Semver
   or the GUI silently drops the file at load time per
   `Preset.cpp:1287`), `filament_vendor`, `filament_type`, and all
   merged content keys.

**Per-printer expansion**: a user filament whose resolved
`compatible_printers` lists N printers produces **N output files** in
the flattened shape — one per printer. Each file scopes
`compatible_printers` and the `@<printer>` suffix to that one printer,
and uses that printer's variant count for padding.

The exported filename matches `name`: `<alias> @<target_printer>.json`.
The user drops the file (or zip contents) into
`~/Library/Application Support/OrcaSlicer/user/<profile>/filament/base/`
or imports it via the GUI's filament-import flow.

The companion `.info` sidecar that the GUI keeps next to each user
filament is **not** generated by this export — the GUI auto-creates it
on first save (`Preset.cpp:518, 522-545`).

`thin`:

1. Read the user's saved file from `_raw_profiles` (the index of all loaded
   profiles, populated at startup and on `/profiles/reload`).
2. Return that dict as-is — no resolution, no key stripping. `inherits` is
   preserved.

The thin form is what the user originally imported (with the import-time
stamps applied: `setting_id`, `instantiation`, possibly `filament_id`). It
matches the GUI's own thin-export shape and is intended for sharing with
another OrcaSlicer install that already has the same parent profile.

### User-only scope

Both endpoints reject any `setting_id` that does not belong to a user
filament. Detection: a profile is "user" when its source directory is
`USER_PROFILES_DIR` (or a subdirectory thereof). Implementation reads from
the existing user-profile bookkeeping established by the deferred-flattening
work — see `_load_user_profiles` and friends in `app/profiles.py`.

A request for an unknown or non-user `setting_id` returns 404 (single) or
is reported in `X-Export-Skipped` with reason `"not_found"` (batch).

### Filename

The download filename comes from the profile's resolved `name` for the
flattened shape, and the raw `name` for the thin shape. (In practice the
two are identical — `name` is identity, not inherited content.)

Sanitization:

- Lowercase.
- Replace any character not in `[a-z0-9._-]` with `_`.
- Collapse runs of `_` to a single `_`.
- Trim leading/trailing `_`.
- If the result is empty, fall back to the `setting_id`.
- Append `.json`.

For the batch zip, if two profiles produce the same sanitized filename,
later occurrences get a `-2`, `-3`, ... suffix before `.json`.

### `GET /profiles/filaments/{setting_id}/export`

- Query param: `shape` ∈ `{"flattened", "thin"}`. Default `flattened`.
  Anything else → 400 with `"Invalid shape; expected 'flattened' or 'thin'"`.
- 404 if `setting_id` is unknown or not a user filament.
- For `shape=thin`: success is `200` with body = pretty-printed JSON. Headers:
  - `Content-Type: application/json`
  - `Content-Disposition: attachment; filename="<safe_name>.json"`
- For `shape=flattened`: success is `200` with body = zip bytes (always a
  zip, even when the filament has only one compatible printer).
  Rationale: a flattened export expands to N files for N compatible
  printers; the response shape stays predictable across N=1 and N>1.
  Headers:
  - `Content-Type: application/zip`
  - `Content-Disposition: attachment; filename="<safe_name>.zip"`
- 500 on `UnresolvedChainError` raised from a broken inheritance chain at
  flatten time. Logged with the broken parent reference. (This is a server
  health issue, not a client error — the user filament was previously
  loaded.)

### `POST /profiles/filaments/export-batch`

- Body (JSON):
  ```
  {
    "setting_ids": ["GFSAxx", "P1234567", ...],
    "shape": "flattened" | "thin"   // optional, default "flattened"
  }
  ```
- 400 if `setting_ids` is missing, not a list, or empty.
- 400 if `shape` is present and not in the allowed set.
- Success: `200` with body = zip bytes built in-memory.
- Headers:
  - `Content-Type: application/zip`
  - `Content-Disposition: attachment; filename="user-filaments-<YYYYMMDD-HHMMSS>.zip"`
  - `X-Export-Skipped`: JSON-encoded object mapping each skipped
    `setting_id` to a short reason string. Header is **omitted** when
    nothing was skipped. Reasons:
    - `"not_found"` — id is unknown or not a user filament.
    - `"unresolved_chain"` — flatten failed because a parent in the chain
      could not be resolved. (Only possible for `shape=flattened`.)
- Zip layout: flat. One `<safe_name>.json` per successfully exported
  output. For `shape=flattened` each input filament expands to one entry
  per compatible printer — the entry filename includes the `@<printer>`
  suffix per the flatten algorithm. For `shape=thin` it is one entry
  per input filament. No nested directories. No `_manifest.json` —
  header carries the skip info.
- If every requested id is skipped, the response is still 200 with a
  zip containing zero entries and `X-Export-Skipped` populated. The
  client decides how to surface this. (Rationale: simpler than a
  conditional 400, and the header conveys enough.)

### Web UI

In `app/web/`:

1. **"Export…" button** in the user-filaments section header, beside the
   existing import controls. Disabled when there are zero user filaments.
2. **Modal** opened by the button:
   - Lists every user filament with a checkbox. **All pre-selected** on
     open. "Select all / Deselect all" toggle. Live count: "N of M
     selected".
   - Each row shows the filament name and, in smaller text underneath,
     its compatible-printer count (e.g. "Compatible with 3 printers").
     This makes the per-printer expansion visible to the user before
     they download.
   - **Shape picker** — two radio buttons, `Flattened` pre-selected:

     > **Flattened** *(recommended)* — Self-contained, AMS-assignable
     > profile. Each filament is exported once per compatible printer
     > because OrcaSlicer scopes AMS-assignment per printer.
     >
     > **Thin** — Preserves the inheritance link to the parent profile.
     > Smaller file, one per filament regardless of compatible-printer
     > count. The receiving OrcaSlicer install must already have the
     > parent profile, and the imported result cannot be assigned to
     > the AMS.

   - **Download button**:
     - Label depends on the resulting payload, not the input count:
       - `Thin` + 1 selected → "Download JSON".
       - All other cases → "Download zip" (because flattened always
         expands to a zip, and any 2+ selection is a zip).
     - Disabled when 0 selected.
     - Routing rule:
       - `shape=thin` + 1 selected → `GET /…/{setting_id}/export?shape=thin`.
       - All other cases → `POST /profiles/filaments/export-batch`.
     - Triggers browser download via `URL.createObjectURL` + temporary `<a>`.
     - On batch response, parse `X-Export-Skipped`. If non-empty, show an
       inline notice in the modal: "Downloaded N file(s). Skipped M
       filament(s): <names>". The download still happens — the user
       keeps what succeeded.
   - **Cancel** button closes the modal without action.

No new CSS framework. Reuse the existing modal styling pattern (the
batch-import modal added in commit `7173d8d`). JS goes in `app.js`
inline-style consistent with the rest of the file.

## Flatten algorithm derivation

The flatten rules above were validated against OrcaSlicer's source by
direct reading of `src/slic3r/GUI/AMSMaterialsSetting.cpp` and
`src/libslic3r/Preset.cpp` (full report retained in conversation
history; key citations called out inline above). The empirical proof
came from three iterative manual exports against a real user filament
("Eryone Matte Imported"):

- **v1** (resolved + strip `inherits`/`base_id`): GUI saw the file on
  sync, but it did not appear in the AMS dropdown. Cause traced to
  vendor-profile markers (`type`, `instantiation`, `setting_id`) and
  absent `inherits` field.
- **v2** (also strip vendor markers, set `inherits=""`, add empty
  GUI-compat fields): still not AMS-assignable, and the filament
  manager froze on open. Cause traced to two issues:
  1. The user-preset name lacked `@` so the AMS popup's
     `name.substr(0, name.find('@')-1)` evaluated to silent-drop.
  2. Per-variant list keys at length-1 against length-N consumers in
     the GUI rendering path — likely the freeze trigger.
- **v3** (rename `name`/`filament_settings_id` to include
  `@<printer>`, scope `compatible_printers` to that one printer,
  pad per-variant lists to the printer's variant count): worked.
  AMS-assignable, no freeze.

The spec encodes the v3 algorithm.

## Component design

### `app/profiles.py`

Add one public helper:

```
def export_user_filament(
    setting_id: str,
    *,
    shape: str,
) -> list[tuple[str, dict[str, Any]]]:
    """Return a list of (safe_filename, profile_dict) entries.

    shape='thin'      → exactly one entry (the saved file).
    shape='flattened' → one entry per compatible printer of the
                        resolved filament (>= 1).
    Raises ProfileNotFoundError if setting_id is unknown or not a user
    filament. Raises UnresolvedChainError (subclass of
    ProfileNotFoundError) if shape='flattened' and the inheritance chain
    cannot be resolved. Caller is responsible for input validation of
    `shape`.
    """
```

Internals:

- Looks up the internal key for `setting_id` (existing `_name_for_slug`
  or equivalent reverse index).
- Confirms the profile's source path is under `USER_PROFILES_DIR`. If
  not, raise `ProfileNotFoundError` with a clear message ("not a user
  filament"). The endpoint maps that to 404.

For `shape="thin"`:

- Read the dict from `_raw_profiles` directly. Return one entry:
  `[(safe_filename, copy_of_dict)]`. The copy ensures the caller cannot
  mutate the index.

For `shape="flattened"`:

- Call `get_profile("filament", setting_id)` (which calls
  `resolve_profile_by_name`); on chain failure raise
  `UnresolvedChainError`.
- Pull the resolved `compatible_printers` list. If empty, raise
  `ValueError("filament has no compatible_printers; cannot flatten for
  GUI export")` — the endpoint maps to 400 with a clear message. (This
  case is rare because vendor parents always set
  `compatible_printers`.)
- For each `printer_name` in `compatible_printers`:
  1. Deep-copy the resolved dict.
  2. Apply the strip set: `inherits`, `base_id`, `type`,
     `instantiation`, `setting_id`.
  3. Set `inherits = ""`.
  4. Build `new_name = f"{alias} @{printer_name}"` where `alias` is the
     original user filament's name with any pre-existing `@…` suffix
     stripped (so re-export is idempotent).
  5. Set `name = new_name`,
     `filament_settings_id = [new_name]`.
  6. Set `compatible_printers = [printer_name]`.
  7. Add empty defaults for `compatible_printers_condition`,
     `compatible_prints`, `compatible_prints_condition` if absent.
  8. Pad per-variant list keys. Per-variant key set is computed as:
     - Pull the printer's variant list from its machine profile (the
       machine profile's `extruder_variant` field; for printers without
       a variant list, treat as length-1 single-variant — no padding
       needed).
     - For each top-level key in the dict whose value is a list and
       whose canonical length matches the variant count, pad by
       replicating the slot-0 value.
     - The key set is determined dynamically — by inspecting the
       resolved dict, not by hardcoding a list. The implementation
       MAY hardcode a known per-variant key set if the dynamic
       inspection turns out to be unreliable in practice; that's an
       implementation choice. The reference list of per-variant keys
       observed in working GUI-native exports is captured in
       `app/normalize.py`'s docstring.
  9. Compute `safe_filename` from `new_name`.
  10. Append `(safe_filename, modified_dict)` to the result list.

Add a helper that resolves a printer name to its machine profile's
`extruder_variant` count:

```
def _printer_variant_count(printer_name: str) -> int:
    """Return the number of extruder variants for a printer profile.

    Looks up the machine profile by name, reads its
    `extruder_variant` field if present, returns the list length.
    Returns 1 (single-variant) if the printer is unknown or the
    field is absent — degrades gracefully rather than failing the
    export.
    """
```

Add a small helper for filename sanitization (`_safe_filename(name:
str, fallback: str) -> str`). Internal — kept private.

No changes to `materialize_filament_import`, `resolve_profile_by_name`,
or the slicing path. The export path is read-only over existing state.

### `app/main.py`

Add two endpoints:

- `GET /profiles/filaments/{setting_id}/export`:
  - Reads `shape` from query string, defaults `"flattened"`.
  - Validates `shape`. 400 on invalid.
  - Calls `export_user_filament(setting_id, shape=shape)` which returns
    a **list** of `(filename, profile)` entries.
  - Distinguishes error cases via three exception types:
    - `ProfileNotFoundError` → 404 (id unknown or not a user filament).
    - `UnresolvedChainError` → 500 (chain failure during flatten).
      `UnresolvedChainError` is a new subclass of
      `ProfileNotFoundError` introduced in `app/profiles.py`. The
      helper raises it for the flatten-time failure (catching the
      resolver's `ProfileNotFoundError` and re-raising) and raises
      plain `ProfileNotFoundError` for the user-scope check. The
      endpoint catches `UnresolvedChainError` first (subclass before
      superclass).
    - `ValueError` (filament has empty `compatible_printers` for
      `shape=flattened`) → 400 with the message body.
  - For `shape="thin"` (single entry): return
    `Response(content=json.dumps(profile, indent=2),
    media_type="application/json", headers={"Content-Disposition":
    'attachment; filename="<safe_name>.json"'})`.
  - For `shape="flattened"` (one or more entries): build zip in-memory
    and return `Response(content=buf.getvalue(),
    media_type="application/zip", headers={"Content-Disposition":
    'attachment; filename="<safe_alias>.zip"'})`. The zip's filename
    derives from the alias (without `@`).
- `POST /profiles/filaments/export-batch`:
  - Parses the JSON body. 400 on bad shape, missing/empty/wrong-typed
    `setting_ids`, invalid `shape`.
  - For each `setting_id` in order, calls the helper. Each call returns
    one or more entries (one for thin, one-per-printer for flattened).
    Collects all successes; collects failures in a `{setting_id:
    reason}` dict:
    - `"not_found"` — user-scope rejection.
    - `"unresolved_chain"` — flatten-time chain failure.
    - `"no_compatible_printers"` — flattened export but the resolved
      filament has empty `compatible_printers`.
  - Builds zip in-memory with `zipfile.ZipFile(io.BytesIO(), "w",
    zipfile.ZIP_DEFLATED)`. Writes each entry as
    `json.dumps(profile, indent=2)` under its (deduplicated) filename.
    Filename collisions across the batch get `-2`, `-3`, … suffixes.
  - Returns `Response(content=buf.getvalue(),
    media_type="application/zip", headers={"Content-Disposition":
    'attachment; filename="user-filaments-<timestamp>.zip"',
    "X-Export-Skipped": json.dumps(skipped)})`. The
    `X-Export-Skipped` header is **omitted** when nothing was skipped.

### `app/models.py`

No new models. The single endpoint returns a binary download; the batch
endpoint accepts a small inline dict. Keeping pydantic out of the export
hot path matches the rest of the file-download surface.

### `app/web/index.html` and `app/web/app.js`

- Add the "Export…" button in the user-filaments section header.
- Add a new modal block in `index.html` mirroring the existing
  import-modal structure — title, body (filament list, shape picker),
  footer (Cancel / Download).
- In `app.js`, add `openExportModal()`, `renderExportList()`,
  `updateExportButton()`, `triggerExport()`. Reuse the existing user
  filament list source.
- Keep code paths short and readable; no new build dependencies. Per
  the user's standing preference, prefer plain DOM manipulation over
  any framework introduction.

### `app/config.py`

Bump `API_REVISION`. The new endpoints are additive, but the branch
already accumulates changes; one combined bump is cleaner.

## Tests

Add `tests/test_export_endpoints.py` (new file):

Single endpoint:

- **Single, thin**: `GET /…/{setting_id}/export?shape=thin` — assert
  200, `Content-Type: application/json`, body equals the saved file
  (`inherits` retained, no parent keys merged).
- **Single, flattened, one compatible printer**: GET on a user filament
  whose resolved chain yields one printer. Assert 200,
  `Content-Type: application/zip`, the zip contains exactly one entry
  with filename `<alias> @<printer>.json`, and that entry has:
  - `name == "<alias> @<printer>"`
  - `inherits == ""` (present, empty)
  - `compatible_printers == ["<printer>"]`
  - `filament_id` non-empty
  - no `type`, no `instantiation`, no `setting_id`, no `base_id`
- **Single, flattened, multiple compatible printers**: GET on a user
  filament whose resolved chain yields N printers. Assert the zip
  contains N entries, one per printer, with correct per-printer
  scoping in each.
- **Single, flattened, per-variant padding**: GET on a user filament
  whose target printer has 2 extruder variants. Assert per-variant
  list keys (`nozzle_temperature`, etc.) in the exported entries are
  length-2.
- **Single, flattened, empty compatible_printers**: GET on a user
  filament whose resolved `compatible_printers` is empty → 400 with
  `"no compatible_printers"`-ish message.
- **Single, default shape is flattened**: GET without the param —
  response is a zip (matches explicit `?shape=flattened`).
- **Single, invalid shape**: `?shape=bogus` → 400.
- **Single, unknown setting_id**: 404.
- **Single, vendor (non-user) setting_id**: 404.
- **Single, broken chain on flattened**: a user filament whose parent
  was deleted between import and export → 500 with the broken parent
  named in the response. (Set up by mutating the index after import.)
- **Single, broken chain on thin**: same setup → 200 (thin doesn't
  resolve).
- **Idempotent re-export**: a user filament whose `name` already
  contains `@<printer>` (from a prior export) — assert the alias
  parsing strips the existing `@…` suffix before re-applying, so the
  exported `name` is `<alias> @<target_printer>`, not `<alias>
  @<old_printer> @<target_printer>`.

Batch:

- **Batch, thin**: POST with N user-filament ids and `shape=thin` →
  zip with N JSON entries, each retaining `inherits`. No
  `X-Export-Skipped` header.
- **Batch, flattened, single-printer filaments**: POST with N user
  filaments each compatible with one printer → zip with N entries,
  each AMS-shaped.
- **Batch, flattened, multi-printer expansion**: one filament
  compatible with 3 printers in the batch → zip contains 3 entries
  for that filament.
- **Batch, partial failure**: include one unknown id, one vendor id,
  one user id → zip contains the user-id's entries, `X-Export-Skipped`
  reports the other two with `"not_found"`.
- **Batch, partial chain failure**: one user filament with a broken
  chain, `shape=flattened` → that id appears in `X-Export-Skipped`
  with `"unresolved_chain"`; other entries succeed.
- **Batch, no_compatible_printers**: a flattened-shape batch where one
  user filament has empty `compatible_printers` → that id appears in
  `X-Export-Skipped` with `"no_compatible_printers"`.
- **Batch, name collisions in zip**: two distinct user filaments whose
  flattened-export filenames sanitize to the same string → zip
  contains both with `-2` suffix on the second.
- **Batch, empty list**: 400.
- **Batch, missing field**: 400.
- **Batch, invalid shape**: 400.
- **Batch, all-skipped**: every id is unknown → 200 with empty zip
  and populated `X-Export-Skipped`.

Algorithm-level unit tests (against `export_user_filament` directly,
without the HTTP surface):

- **Strip set is exact**: assert `type`, `instantiation`, `setting_id`,
  `base_id` are absent and `inherits` is `""` in every flattened
  entry.
- **Empty defaults**: assert `compatible_printers_condition`,
  `compatible_prints`, `compatible_prints_condition` are present (with
  empty values) when the input lacked them, and untouched when the
  input had them.
- **Variant-count lookup**: a printer whose machine profile has
  `extruder_variant` of length 1 → no padding occurs. A printer
  unknown to the index → degrades to length-1 (no padding), no
  exception.

Reuse the existing `_profile_test_helpers` for test fixture setup
(user filament loading, vendor parent registration). Add a small
machine-profile fixture helper if one isn't already present.

## Risks and mitigations

- **Per-variant key set drifts between OrcaSlicer versions**: the GUI's
  per-variant key set isn't a stable API. New versions may add or
  remove per-variant keys, which would leave our padding incomplete
  (length-1 against length-N consumers — the same condition that
  caused the v2 freeze). Mitigation: derive the set dynamically from
  the resolved profile rather than hardcoding. Add a smoke test that
  imports a freshly-exported file into a current OrcaSlicer build
  before each release.
- **Variant-count lookup misses**: if the target printer's machine
  profile is unknown to our index (e.g., a vendor profile that didn't
  load), we degrade to length-1 single-variant. The export will load
  in the GUI but may underflow on a multi-variant printer. Mitigation:
  test fixture exercises both paths; document the degrade behavior
  in the helper docstring; surface a warning log when the lookup
  misses.
- **`X-Export-Skipped` header size**: in pathological cases (hundreds
  of skipped ids), the header could exceed reasonable limits.
  Mitigation: this surface is for user-filament catalogs that are at
  most low-double-digits in practice. If we hit a real ceiling, switch
  to embedding `_manifest.json` in the zip as a follow-up.
- **Thin export of a profile whose parent was renamed between import
  and export**: the thin file imports as a broken reference on the
  receiving side. Mitigation: the modal text already names this risk
  ("the receiving OrcaSlicer install must already have the parent
  profile"). No code-level mitigation.
- **User-only scope check missing a code path**: if some user filament
  ends up indexed without a clear "user" marker, it would 404 on
  export. Mitigation: rely on the same source-tracking the
  deferred-flattening branch already established and exercise it in
  tests.

## Open questions

None. The flatten algorithm was confirmed empirically (v3 manual
export → AMS-assignable in OrcaSlicer GUI, no freeze).

## Out of scope (for this change)

- Process and machine exports.
- Server-side persistence of export selections / history.
- Embedding a `_manifest.json` inside the zip.
- Authentication / per-user scoping. The API currently has no auth
  surface; export inherits that posture.
- Migrating thin files to flattened (or vice versa) on disk.
