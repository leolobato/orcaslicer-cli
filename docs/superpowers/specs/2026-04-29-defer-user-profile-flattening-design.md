# Defer flattening of user-imported profiles to slice time

## Goal

Store user-imported filament and process profiles in their raw, GUI-shaped
form (with `inherits` preserved), and resolve the inheritance chain at
slice time. This lets users drop in OrcaSlicer GUI exports verbatim
(modulo a small import-time stamp) instead of relying on this API to
pre-merge them with their parent profile.

## Motivation

Today, `materialize_filament_import` and `materialize_process_import`
(`app/profiles.py:244` and `app/profiles.py:321`) merge each import payload
with its parent and strip `inherits` before writing it to
`USER_PROFILES_DIR`. The CLI consumer of those profiles is OrcaSlicer
itself, but at slice time we already use `resolve_profile_by_name` to
flatten vendor profiles on demand — the chain machinery exists. Pre-flattening
on save makes user profiles a copy of their parent at import time, which
diverges from the GUI's storage shape and forces clients to call
`/profiles/filaments/resolve-import` even for a thin export they could
otherwise upload as-is.

## Scope

- `materialize_filament_import` — raw form on save.
- `materialize_process_import` — raw form on save.
- `resolve_profile_by_name` — always raise `ProfileNotFoundError` on a
  missing parent (no silent partial profiles).
- Listing-side iterators that today silently skip resolution failures —
  wrap with `try/except ProfileNotFoundError` plus `logger.warning` to
  preserve "skip" semantics while surfacing broken chains in logs.

Out of scope: machine profiles (no user import path), changes to the slice
pipeline beyond the resolver call site, schema migrations for existing
flattened files in `/data` (handled by automatic backwards compatibility,
see below).

## Behavior changes

### Import-time materialization

`materialize_filament_import(data)` becomes:

1. Validate that `name` is a non-empty string. Reject otherwise.
2. Synthesize `setting_id` from `name` if not provided. Validate it is a
   non-empty string.
3. If `data["inherits"]` is set, look up the parent via
   `_resolve_filament_parent_ref`. Raise `ProfileNotFoundError` if it does
   not resolve to a known filament profile. Do **not** merge.
4. Set `instantiation: "true"` if not already present.
5. Stamp a stable `filament_id`. `filament_id` is the AMS identity for
   the spool; if it were allowed to inherit from the parent, every
   "clone" of e.g. *Bambu PLA Basic* would share the parent's id and
   AMS assignment would collide. The rule:
   - If `data` directly contains a non-empty `filament_id` (not
     inherited), keep it verbatim.
   - Otherwise, generate `"P" + md5(logical_name)[:7]` from
     `_logical_filament_name(name)`, with timestamp-based collision
     fallback (existing helper `_generate_custom_filament_id`). Write it
     into the payload.
   The stamped `filament_id` lives in the saved raw file. At resolve
   time, `merged.update(child)` ensures it wins over the parent's
   `filament_id`.
6. Validate `filament_id` uniqueness against AMS scope. AMS assignment
   is per-printer, so the constraint is: two profiles may share
   `filament_id` **iff** their resolved `compatible_printers` sets are
   disjoint. The check:
   - Resolve the new import's `compatible_printers` by walking its
     `inherits` chain (if the field isn't set on the import payload
     itself, it's inherited from the parent). The result is a set of
     printer profile names.
   - Look up every other indexed filament profile that shares the same
     `filament_id`. For each match, compute its resolved
     `compatible_printers` set the same way.
   - **Disjoint** sets — allowed. This covers the canonical "same
     physical spool, different machine variant" case (e.g. "Bambu PLA
     Basic @BBL A1M" and "@BBL X1C") because A1M and X1C are different
     printers.
   - **Overlapping** sets — raise `ValueError` (surfaced as 400 by the
     endpoint) with a message naming the conflicting profile, the
     colliding `filament_id`, and the overlapping printers. This
     catches a user pasting in a vendor id (e.g. `GFA00` for an A1M
     filament colliding with Bambu's `GFA00`) and accidental hash
     collisions on overlapping printer sets.
   - **Either side has empty resolved `compatible_printers`** — fail
     closed. Empty effectively means "all printers" in OrcaSlicer
     semantics; treating it as a global match prevents a thin user
     profile from claiming a vendor id by under-scoping. In practice
     this is rare because thin imports inherit `compatible_printers`
     from a non-empty parent.
   - **Replace case** (same `setting_id` as the existing profile being
     overwritten) — the existing entry is excluded from the comparison
     so a profile can be re-imported under itself.
   `_generate_custom_filament_id` already self-resolves
   different-logical-name collisions during generation, so step 6 fires
   in practice when the caller supplied `filament_id` directly.
7. Return the (possibly mutated) input dict as-is. `inherits`, `from`,
   `filament_settings_id`, `base_id`, and any other fields are preserved
   exactly as the caller supplied them.

`materialize_process_import(data)` follows the same pattern:

1. Validate `name`.
2. Synthesize `setting_id` from `name` if missing.
3. If `inherits` is set, validate the parent via
   `_resolve_process_parent_ref`. Raise `ProfileNotFoundError` if missing.
4. Set `instantiation: "true"` if missing.
5. Return as-is.

What the new code does **not** do (relative to today):

- No merging with the parent profile.
- No stripping of `inherits` or `base_id`.
- No stamping of `from: "User"`.
- No stamping of `filament_settings_id` or `print_settings_id`.
- No `filament_type` presence requirement on the input payload — it
  resolves through the chain at slice time.

`filament_id` stamping is preserved from today's behavior (see step 5
above) — it is an identity field for AMS, not an inheritance artifact.

### Strict resolution by default

`resolve_profile_by_name` in `app/profiles.py:565` becomes strict
unconditionally — there is no `strict` parameter:

- If `profile.get("inherits")` is set but `_resolve_parent_key` returns
  `None`, raise `ProfileNotFoundError` with a message that names both
  the child profile and the missing parent reference.
- If the recursive `resolve_profile_by_name(parent_key)` raises,
  propagate.
- On success, the merged dict is cached in `_resolved_cache` as today.

The two listing-side iterators that today silently skip resolution
failures continue to skip them — but now via an explicit
`try/except ProfileNotFoundError` plus `logger.warning`, so broken
chains are surfaced in logs:

- `_iter_known_filament_names_and_ids` (`app/profiles.py:187-207`):
  wrap the `resolve_profile_by_name` fallback. On
  `ProfileNotFoundError`, log a warning naming the profile and the
  missing parent, then `continue` (preserving today's "skip" semantics
  for the id-collision iteration).
- `_is_ams_assignable_filament` and any other listing-side resolution
  call site discovered during implementation: same pattern. On
  resolution failure, log a warning and report the profile as
  not-assignable / exclude it from the listing.

Why "skip with a log" for these two and not "raise":

- **Slice path** uses an invalid profile to produce G-code → concrete
  user harm. Must fail loudly. `_resolve_by_slug` /
  `get_profile` propagate the `ProfileNotFoundError` directly.
- **Listing path** would otherwise take the entire `/profiles/filaments`
  endpoint down because of one broken vendor profile elsewhere in the
  bundle. The right answer is to exclude the unusable profile, not
  hide the rest of the picker.
- **Import collision iteration** would otherwise cause a user's custom
  filament import to 500 because of an unrelated broken vendor profile.
  Same reasoning.

`_resolve_by_slug` (`app/profiles.py:642`) calls
`resolve_profile_by_name` and propagates the `ProfileNotFoundError`.
`get_profile` is the public entry point used by the slice path
(`app/slicer.py:1253-1257`), so `/slice` fails fast with a clear error
instead of silently feeding a malformed profile to OrcaSlicer.

### `/profiles/{filaments,processes}/resolve-import` — preview only

The endpoint becomes a **pure preview**. It does not produce the payload
that should be sent to `/profiles/{filaments,processes}` for save;
clients should send their original raw payload to the POST endpoint
directly (see "Client migration" below).

Field rename: `resolved_payload` → `resolved_profile` on both
`FilamentProfileImportPreview` and `ProcessProfileImportPreview`. The
new field contains the **fully merged** form — the result of resolving
the inheritance chain in memory, with stamping applied (`setting_id`,
`instantiation`, and for filaments also `filament_id`). This is the
shape callers can show to the user to answer "what will this profile
look like once resolved?" — equivalent to `GET
/profiles/filaments/{setting_id}` after save.

Other top-level fields on the preview model (`filament_type`,
`filament_id`, `name`, `setting_id`, `inherits_resolved`) continue to
be populated from the resolved view.

This is a **breaking API change** for any client reading
`resolved_payload`. Bumped via the `API_REVISION` constant when this
ships.

### `/profiles/{filaments,processes}` POST — accepts raw

The endpoint accepts a raw payload (file upload contents or a
hand-built form payload with `inherits`) and saves the lightly-stamped
raw form to `USER_PROFILES_DIR`. The response model
(`FilamentProfileImportResponse`, `ProcessProfileImportResponse`) is
unchanged: `filament_type` and `filament_id` are populated by resolving
the chain after save.

Clients that previously POSTed `resolved_payload` from the preview
endpoint must switch to POSTing the raw payload they had in hand
before calling preview. See "Client migration" below.

### Backwards compatibility

Existing flattened user profiles in `/data` continue to load and resolve
without modification. They have no `inherits` field, so
`resolve_profile_by_name` returns them as-is. No migration script is
needed; new imports simply use the new format.

## Affected clients

- **bambu-gateway** — only consumes `GET /profiles/filaments/{setting_id}`
  (`app/slicer_client.py:271-287`). Unaffected.
- **bambu-spool-helper** — needs a small change to the upload-import
  flow only. See "Client migration" below.

## Client migration (bambu-spool-helper)

spool-helper has two filament import flows:

**Flow A — uploaded JSON** (`app/routers/web.py:1111-1243`):

Today: read upload → POST `/resolve-import` → bind `payload =
resolved_payload` → POST that to `/profiles/filaments`.

After: read upload → POST `/resolve-import` → use `resolved_profile`
to read `filament_type` for UX validation only → POST the **original
upload** (or `payload_json` from a pending re-submission) to
`/profiles/filaments`. Two-line change at `web.py:1198` (read from
`resolved_profile` instead of `resolved_payload`) and `web.py:1213`
(POST the original upload, not the merged blob).

The "user must pick a filament type" branch is unchanged — it still
fires when the resolved view lacks `filament_type`, which is rare
because thin GUI exports typically inherit a parent that defines it.

**Flow B — "create from filament" form** (`app/routers/web.py:1296-1389`):

Already POSTs a raw payload with `inherits` directly to
`/profiles/filaments`. **No change required.** Today's API merges this
into a flat profile on save; after the refactor it's saved raw, which
is the desired behavior.

**Process imports** (`app/routers/web.py:1072-1082`):

Today: `resolved_payload` is forwarded verbatim to
`/profiles/processes`. Same two-line change as Flow A: read
`resolved_profile` for inspection if needed, POST the original upload
to `/profiles/processes`.

## Component design

### `app/profiles.py`

- Replace the body of `materialize_filament_import` per the rules above.
  `_generate_custom_filament_id` and `_logical_filament_name` are still
  used for the `filament_id` stamping path. `_has_direct_filament_id` is
  still used to decide whether to keep the caller's id or generate one.
  Drop only helpers that are genuinely unused after the change.
- Add a helper to compute the resolved `compatible_printers` set for a
  given profile: read it from the input payload if present; otherwise
  walk `inherits` (using the existing chain machinery) until a value is
  found or the chain ends. Returns a `set[str]`.
- Add a helper that, given a candidate `(filament_id, compatible_printers)`
  and an optional `exclude_setting_id`, scans loaded filament profiles
  for any conflicting AMS-scope match — used by step 6 of
  `materialize_filament_import`. The replace case passes the existing
  setting_id so the profile being overwritten is excluded.
- Replace the body of `materialize_process_import`.
- Make `resolve_profile_by_name` always strict: raise
  `ProfileNotFoundError` instead of falling back to a partial profile
  when a parent is missing or its recursive resolution fails. No new
  parameter; the change is unconditional.
- Wrap the two listing-side resolution call sites
  (`_iter_known_filament_names_and_ids` and the listing path of
  `_is_ams_assignable_filament`) with
  `try/except ProfileNotFoundError` + `logger.warning(...)` + skip /
  return False. Search for any other tolerant call sites during
  implementation and apply the same wrap.
- The `_load_user_profiles` synthesis of `setting_id` from `name` at load
  time stays for safety.

### `app/main.py`

- `_read_filament_import_body` and `_read_process_import_body` keep the
  same shape but the returned `data` is now raw with stamps.
- `resolve_filament_import` and `resolve_process_import` populate the
  preview model by:
  1. Running materialization (validates parent, stamps `setting_id` /
     `instantiation` / `filament_id`).
  2. Resolving the chain in memory using `resolve_profile_by_name` over
     a temporary registration — or, more simply, building the merged
     dict ad-hoc by walking `inherits` once. (Implementation detail; the
     simplest path is fine.)
  3. Placing the **merged** form into the new `resolved_profile` field.
  4. Populating top-level fields (`filament_type`, `filament_id`, etc.)
     from the merged form.
- `import_filament_profile` and `import_process_profile` save the raw
  payload. To populate `filament_type`/`filament_id` in
  `FilamentProfileImportResponse`, resolve the chain once after save
  (using `get_profile`) and read those fields off the resolved view.

### `app/models.py`

- `FilamentProfileImportPreview.resolved_payload` → renamed to
  `resolved_profile`. Description updated to "Fully merged filament
  profile (inheritance resolved) — informational. Send the original raw
  payload to `POST /profiles/filaments`, not this field."
- `ProcessProfileImportPreview.resolved_payload` → renamed to
  `resolved_profile`. Same description treatment.

### `app/config.py`

- Bump `API_REVISION`. The `resolved_payload` → `resolved_profile`
  rename is a breaking change for clients reading the preview
  response.

### Tests

Update / add in `tests/`:

- `test_import_endpoints.py`:
  - Update existing assertions: the preview response's field is now
    `resolved_profile` (renamed) and contains the **merged** form, not
    the raw form.
  - Add: POST a thin GUI-shaped fixture to `/profiles/filaments` (raw
    payload, with `inherits`). Read the saved file under
    `USER_PROFILES_DIR` and assert it still contains `inherits` and
    only the differential keys plus stamped `setting_id` /
    `instantiation` / `filament_id`. Assert that the saved file is
    **not** flattened.
  - Add: round-trip — POST raw, then GET
    `/profiles/filaments/{setting_id}` and assert the resolved view
    contains parent values.
  - Add: round-trip — assert the resolved view's `filament_id` equals
    the stamped child id, **not** the parent's id.
  - Add: `/resolve-import` returns `resolved_profile` containing
    parent-inherited values (e.g. `filament_type` from the parent).
  - Add: import with a directly-supplied `filament_id` keeps it
    verbatim in the saved file.
  - Add: import with a directly-supplied `filament_id` whose resolved
    `compatible_printers` overlaps an existing profile sharing that id
    returns 400 with a message naming the conflict and the overlapping
    printers (covers "user pastes vendor `GFA00` for an A1M-compatible
    profile").
  - Add: import with a directly-supplied `filament_id` matching a
    profile that targets disjoint printers succeeds (e.g. `@BBL A1M`
    sibling to an existing `@BBL X1C` with the same id).
  - Add: import where the resolved `compatible_printers` is empty and
    the id collides with another profile fails (fail-closed).
  - Add: replace=true on an existing user profile keeps the same
    `filament_id` and is not blocked by the uniqueness check (the
    profile being replaced is excluded from the comparison).
  - Add: import with unknown `inherits` returns 400.
- `test_process_import.py`: parallel coverage for processes.
- New unit test for `resolve_profile_by_name`:
  - Raises `ProfileNotFoundError` when `inherits` references a name
    that is not in the index, with a message naming both the child and
    the missing parent.
  - Raises when an intermediate parent in a multi-level chain is
    missing.
  - Returns the merged dict on a successful chain.
- New unit test for the listing-side wrap behavior:
  - When a vendor filament profile has a broken `inherits` chain,
    `/profiles/filaments` returns the rest of the catalog (the broken
    profile is excluded).
  - A `logger.warning` is emitted naming the broken profile.
  - A custom filament import succeeds even when an unrelated vendor
    profile in the index has a broken chain (collision iteration skips
    the broken profile).
- New regression test: a pre-existing flattened user profile (no
  `inherits`) still loads and resolves to itself.
- AMS-assignability test: a thin imported filament inheriting from a
  vendor parent that has a `filament_id` is reported as AMS-assignable.

## Risks and mitigations

- **Preview ↔ saved divergence.** The preview surface still resolves the
  chain to populate `filament_type`/`filament_id`, while the file on
  disk is raw. Mitigation: clearly document the field semantics in
  `models.py` and in this spec; keep both shapes covered by tests.
- **Hidden reliance on stamped fields.** Some downstream code may read
  `from: "User"` or `filament_settings_id` directly from the saved file
  (these are no longer stamped at import). Mitigation: search the
  codebase for those reads before implementation and migrate them to
  use `resolve_profile_by_name` if present.
- **Strict resolution surfacing pre-existing chain bugs.** Today's
  silent fallback may be hiding real broken-parent scenarios in vendor
  profiles. After this change, slicing fails loudly on any such
  profile (correct), and listings exclude it (with a warning logged).
  Mitigation: monitor logs for `unresolved inherits chain` warnings
  after deploy; treat them as a P2 to triage. Because tolerant call
  sites still skip broken profiles, deploy does not break the listing
  endpoint or the import endpoint even if latent chain bugs exist.

## Open questions

None.

## Out of scope (for this change)

- Migrating existing flattened files in `/data` to the new raw form.
- Changes to `FilamentProfileImportResponse` /
  `ProcessProfileImportResponse` response models. (Only the *preview*
  models are renamed.)
- Changing the slice pipeline beyond the `get_profile` call site.
- Eliminating the listing-side skip-on-broken-chain behavior. Listings
  exclude unresolvable profiles with a warning log; making them fail
  loudly would deny the user the rest of the catalog because of one
  bad profile elsewhere.
