# Copies feature — design

**Date:** 2026-05-14
**Status:** Approved (skipping section-by-section review per user request)
**Repos touched:** `orcaslicer-cli`, `bambu-gateway`, `bambu-gateway-ios`

## Goal

Let an iOS user request that an uploaded plate be sliced as N independent prints of each object, the way OrcaSlicer GUI does via *Object → Set number of copies*. The slicer arranges the duplicated instances on the bed and produces a single multi-instance G-code job.

## User-facing behavior

- iOS slice config screen gains a **Copies** stepper row near the top (above the filament rows, alongside Plate Type / Process / Filaments). Default `1`. Tapping the number opens a numeric keypad. Range `1..100`.
- `copies = 1` (default) keeps existing behavior bit-identical.
- `copies > 1` produces N instances of *each* `ModelObject` already on the plate. A 2-object plate with `copies = 4` → 8 items on the bed (4 of A, 4 of B), repacked together. This matches the GUI's `add_instance` semantics — the GUI has no "duplicate the whole layout" primitive either.
- If the requested count cannot be packed onto the bed, the slice fails with a clear error. The user reduces and retries.

## Wire format

Single new field `copies: int` threaded through three layers. Default `1`.

```
iOS                 SliceRequest.copies: Int = 1     (Swift)
   ▼
bambu-gateway       SliceRequest.copies: int = 1     (pydantic) — passthrough
   ▼
orcaslicer-cli      SliceTokenRequest.copies: int = 1   (pydantic, /slice/v2)
   ▼
orca-headless       SliceRequest::copies (int, default 1)   (C++ struct, JSON-deserialized)
```

**Bounds:** `1..100` enforced at every layer (defense in depth). The GUI's cap is 1000, but mobile-initiated requests for hundreds of copies are almost always typos and a 1000-copy slice would take many minutes regardless. We can raise the cap later if a real use case shows up.

**Naming rationale:** `copies` matches the GUI vocabulary (`set_number_of_copies`, "Number of copies") and avoids collision with the `instances` term that already appears in 3MF parsing.

## Server-side behavior (where the actual work happens)

All instance creation and arrangement happens in the C++ binary (`cpp/src/slice_mode.cpp`), mirroring the GUI's primitives 1:1.

### Reference paths in the GUI source

- `Plater::increase_instances` — `OrcaSlicer/src/slic3r/GUI/Plater.cpp:14255` (the duplication primitive).
- `Plater::find_new_position` — `OrcaSlicer/src/slic3r/GUI/Plater.cpp:7362` (the arrange call site we mirror).
- `arrangement::arrange(movable, fixed, build_volume().polygon(), arr_params)` — `Plater.cpp:7389`.

### Inserted step in `run_slice_mode`

After `Model::read_from_file` and before `auto_center_on_plate`, when `req.copies > 1`:

1. **Duplicate instances.** For each `ModelObject* obj` in `model.objects`:
   - Capture the last `ModelInstance` as the template (offset, scaling, rotation, mirror).
   - Loop `copies - 1` times calling `obj->add_instance(template_offset + small_visual_offset, template_scale, template_rot, template_mirror)`. The visual offset stack matches `Plater::increase_instances` (~5% of bed size, accumulated) so that `arrangement::arrange` sees N distinct items rather than one degenerate stack.
2. **Arrange.** Build `arrangement::ArrangePolygons` from every `ModelInstance` (mirroring `Plater::find_new_position`), call `arrangement::arrange(movable, /*fixed=*/{}, printable_area_polygon, ArrangeParams{})`, then walk results and either:
   - Apply translation+rotation to each instance via `apply_arrange_result`, **or**
   - If any item has `!is_arranged() || bed_idx != 0` → fail with `error_code = "copies_dont_fit"`, `error_message = "Cannot place N copies on bed"`, return `1`. No partial slice.
3. **Skip auto-center.** Arrange already centers the packed result; `auto_center_on_plate` is a no-op when `copies > 1` (gated explicitly so we don't double-translate).

When `req.copies == 1` (or unset), none of this code runs and the path is identical to today.

### Validation side-effects we get for free

`Print::validate()` consumes `ModelInstance::arrange_order`, which `Print.cpp:881` assigns when objects flow through the print pipeline (CLAUDE.md documents this — fixture 07 broke when we tried to short-circuit it). The `add_instance` path already populates this correctly because we go through the same `ModelObject::add_instance` libslic3r calls the GUI uses.

### Response echo

The server echoes `body.copies` back to the caller so clients can confirm the server honored the field:
- `/slice/v2` JSON response: `"copies": <int>`
- `/slice-stream/v2` SSE `result` event payload: `"copies": <int>`

No header (`X-Copies-Requested` was considered and rejected — every consumer parses the JSON anyway, and adding a header to a streaming SSE response is awkward). No "placed" echo — failure mode is hard-fail, so requested == placed on success.

## Gateway behavior (`bambu-gateway`)

Pure passthrough. `SliceRequest` (the gateway's own pydantic model used by iOS) gains `copies: int = 1`. `slicer_client.py::slice_v2` includes it in the JSON body sent to `orcaslicer-cli /slice/v2`. No domain logic.

The gateway's existing `_should_auto_center_for_machine` (cross-printer detection) is unchanged. When `copies > 1`, the wrapper ignores `auto_center` anyway (arrange handles centering), so the flag's value becomes irrelevant on those requests.

## iOS behavior (`bambu-gateway-ios`)

### Model

- `SliceRequest.copies: Int = 1` (Codable). Encode `1` explicitly (don't elide) to match server expectation.

### UI

- New `Stepper` row on the slice config screen, placed above the Filaments section and below Plate Type. Label: **Copies**. Range `1...100`. Tapping the value opens a numeric keypad (`TextField` with `.keyboardType(.numberPad)` plus stepper buttons).
- When `copies == 1` the row reads `Copies   1` (no plural special-casing — keep it boring).
- When the slice fails with the `copies_dont_fit` server error, surface the message inline below the Copies row in the existing error styling.

### Telemetry

Existing slice-job result already records what the user submitted; no new event needed.

## Errors

| Layer | Code | Cause | Surfaced where |
|---|---|---|---|
| iOS | (validation) | User typed `< 1` or `> 100` | Stepper clamps; no request sent |
| Gateway | `400 invalid_copies` | Field out of range from a non-iOS client | JSON response |
| orcaslicer-cli | `400 invalid_copies` | Same | JSON response |
| orca-headless | `copies_dont_fit` | `arrangement::arrange` couldn't place every item on bed 0 | SSE error event → propagated as 500 with `error_code` |

## Testing

### orcaslicer-cli (Python)
- `tests/test_slice_request_parsing.py` — extend: `copies` defaults to `1`, accepts `1..100`, rejects `0`, `101`, negative, non-int.
- `tests/integration/test_copies.py` (new) — run against the dev container:
  - `copies=1` produces byte-identical G-code to today's pipeline (regression guard).
  - `copies=4` of a small benchy fixture on A1 mini → succeeds, output 3MF has 4 `<model_instance>` per object.
  - `copies=20` of a fixture too large to pack → fails with `error_code=copies_dont_fit`.

### orca-headless (C++)
- The arrange call site is hard to unit-test in isolation; rely on the integration tests above.
- Add a small fixture (`fixtures/copies/small.3mf`) so the integration test isn't dependent on the existing benchy fixture sizing.

### bambu-gateway
- `tests/test_slice_passthrough.py` — extend: `copies` field is forwarded to `/slice/v2` unchanged; default `1` when absent.

### bambu-gateway-ios
- Snapshot test for the slice config screen with `copies = 1` and `copies = 8` (visual confirmation of the row).
- Unit test on `SliceRequest` Codable encoding includes `copies`.

## Out of scope (deferred)

- **Per-object copy counts.** GUI supports it; iOS doesn't need it for v1.
- **"Fill bed"** (the GUI's `fill_bed_with_copies`). Useful but separate UX (no count input, max-fit search).
- **Suggested-max in the error.** We considered binary-searching after a failed arrange; rejected as cost > value for v1. Re-evaluate if support tickets show users guess-and-retrying.
- **Pre-flight client-side fit estimate.** Same — wait for evidence of real friction.

## Open risks

- **Arrange link cost.** `arrangement::arrange` lives in libslic3r; should already be linked into our `orca-headless` binary (the GUI links it) but worth verifying in the first build that we don't pull a new transitive dep.
- **Wipe tower interaction.** GUI's `find_new_position` adds the wipe-tower polygon as a fixed item before arranging. For multi-filament BBL plates with `enable_prime_tower = true`, we'll want the same. First implementation can omit this for the single-filament case and add it when a multi-filament copies fixture is exercised.
- **Per-instance config keys.** Some 3MFs carry per-instance `arrange_order` or other instance-scoped metadata. Duplicating the last instance carries its config forward, which is what we want — but worth grepping `ModelInstance` for any field that *shouldn't* be copied (e.g. unique identifiers).
