# Deferred Work

Items that were discussed and consciously deferred. Each entry records what we'd revisit, why we didn't do it now, and what would trigger picking it up.

## Sparse `filament_profiles` backfill vs. cross-printer 3MFs

**Where:** `app/slice_request.py:parse_filament_profile_ids()` — when a client sends a sparse dict (e.g. `{"0": {...}}`), we read `filament_settings_id` out of the 3MF and overlay the client's overrides on top, returning a full list where unspecified slots inherit the 3MF's originals.

**The issue:** a 3MF authored for printer *X* carries filament profile names (e.g. `Generic PLA @BBL P1P`) that don't exist in the catalog of a different target printer *Y*. When the client sends a sparse dict for *Y*, the backfilled slots reference profiles that can't resolve via `get_profile("filament", …)` and the request 400s. The new trailing-unused-slot trim (`_trim_unused_filament_ids`) drops the tail end, which covers the common case. The uncovered sub-case is cross-printer 3MFs with *interior* holes in the used-slot set — e.g. `slice_info.config` reports slots `{0, 2}` used, slot 1 is unused but interior, so the trim can't drop it and the backfill injects an unresolvable profile there.

**Why it's deferred:** we haven't actually observed this in the wild. All real failures so far are "trailing unused slots," which is solved. Fixing this properly is a design choice with trade-offs, not a one-line change.

**Options when we revisit:**

1. Fall back to a safe default (e.g., copy slot 0's resolved profile) when a backfilled slot doesn't resolve in the target printer's profile set. Preserves slot indices for objects that bind to specific slots. Opaque to the caller.
2. Deprecate the sparse format — require the client to send a full list. Simplest server, but breaks the "convenience partial override" pattern and forces every caller (including bambu-gateway and iOS) to always know every slot.
3. Return a structured 400 that names the unresolvable slot and lets the client retry with an explicit override for just that slot.

**Trigger to revisit:** first report of a cross-printer 3MF with non-contiguous used slots failing with `filament profile … not found` after v8.

## ~~Why OrcaSlicer CLI fails on "floating regions" when the GUI doesn't~~ — resolved

**Real root cause:** `bambu-gateway/app/parse_3mf.py:_flatten_3mf` was corrupting multi-file 3MFs before they reached us. Its regex-based mesh extraction grabbed only the first `<mesh>` block from each external model file, and its per-object loop overwrote a single dict key on every iteration — so an articulated 60-part model collapsed to one tiny stub (224,850 triangles → 96). OrcaSlicer then correctly flagged the disconnected stub as having floating regions. Additionally, gateway's sanitize stripped `machine_*` / `printable_area` / `bed_*` keys which — empirically — triggered an OrcaSlicer SIGSEGV on the unflattened multi-file structure.

**Fix (in bambu-gateway):** removed `sanitize_3mf` entirely. The gateway no longer mutates 3MFs before forwarding. Rationale: the broken flatten was removed; the machine-key stripping was counterproductive; the clamp rules duplicated orcaslicer-cli's own `_sanitize_3mf` + process-profile clamping. All sanitation now lives in orcaslicer-cli where it runs against the full context (machine profile, plate selection, slice_info.config).

**Supporting v9 change (orcaslicer-cli):** `_sanitize_3mf` now also rewrites `printer_model` / `printer_settings_id` in the 3MF to match the target `machine_profile`, avoiding OrcaSlicer's "foreign vendor" branch (`found 3mf from other vendor, split as instance`). This wasn't the Octopus fix, but does make genuine cross-printer slicing more robust.
