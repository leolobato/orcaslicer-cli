# STL Preview Import Design

**Project name:** orcaslicer-headless

## Goal

Support printing STL files through a preview-first workflow. STL uploads must be imported with OrcaSlicer/libslic3r behavior, arranged on the selected printer bed, previewed in `bambu-gateway`, and only then materialized to a 3MF for the existing slice/print flow.

STL files contain geometry only. They do not carry plate placement, printer/process/filament selections, thumbnails, or 3MF project settings. The feature therefore creates an intermediate draft session instead of pretending STL can be sliced like a project 3MF.

## Decisions

- Preview is mandatory for STL. The user always sees the arranged draft before slicing.
- Gateway web renders the original STL client-side with Three.js.
- `orcaslicer-headless` owns the authoritative import, orientation, arrangement, and draft 3MF generation.
- Auto-orient is user-selectable. It is not forced by default.
- V1 exposes preset controls only: auto-orient, rotate Z 90 degrees, rotate Z -90 degrees, center, arrange, reset.
- V1 does not include freehand drag/rotate, arbitrary face-pick lay-flat, scaling, or a general 3MF scene viewer.

## GUI Parity Anchors

The implementation must use OrcaSlicer/libslic3r paths rather than Python geometry reimplementation:

- `../OrcaSlicer/src/libslic3r/Model.cpp:241` and `:277-278` route `Model::read_from_file` for `.stl` through `load_stl`.
- `../OrcaSlicer/src/slic3r/GUI/Plater.cpp:6406-6421` shows the GUI loading files through `Model::read_from_file`.
- `../OrcaSlicer/src/slic3r/GUI/Plater.cpp:6572-6573` grounds loaded model objects with `ensure_on_bed`.
- `../OrcaSlicer/src/libslic3r/Model.cpp:699-717` implements `Model::center_instances_around_point`.
- `../OrcaSlicer/src/slic3r/GUI/Jobs/OrientJob.cpp:161-181` drives auto-orient through `orientation::orient`.
- `../OrcaSlicer/src/slic3r/GUI/Jobs/OrientJob.cpp:225-245` builds the per-instance orientation mesh and applies the returned rotation.
- `../OrcaSlicer/src/slic3r/GUI/Jobs/ArrangeJob.cpp:441-567` is the arrange pipeline to mirror: initialize params, build arrange polygons, update inflation/axis alignment, shrink bed points, then call `arrangement::arrange`.

Existing `app/stl_to_3mf.py` should not be the foundation for this feature. It manually parses STL and emits a minimal 3MF, which creates drift from the GUI import path.

## orcaslicer-headless API

### `POST /stl/import`

Multipart upload with:

- `file`: `.stl`
- `machine_id`: target machine profile setting_id
- `process_id`: target process profile setting_id, required so auto-orient and arrange can use the same process-dependent settings the GUI sees
- `plate_type`: optional API plate type
- `auto_orient`: bool, default `false`
- `arrange`: bool, default `true`
- `center`: bool, default `true`

Response:

```json
{
  "draft_token": "string",
  "source_filename": "part.stl",
  "bed": {
    "width": 256,
    "depth": 256,
    "printable_area": [[0, 0], [256, 0], [256, 256], [0, 256]]
  },
  "objects": [
    {
      "id": "0",
      "name": "part.stl",
      "transform": {
        "offset": [128.0, 128.0, 0.0],
        "rotation": [0.0, 0.0, 0.0],
        "scale": [1.0, 1.0, 1.0]
      },
      "bbox": {
        "min": [90.0, 95.0, 0.0],
        "max": [166.0, 161.0, 42.0]
      },
      "printable": true
    }
  ],
  "warnings": [],
  "actions": ["auto_orient", "rotate_z_90", "rotate_z_minus_90", "center", "arrange", "reset"]
}
```

`draft_token` points to a cached draft model inside `orcaslicer-headless`. It is not a stable persisted project ID.

### `POST /stl/{draft_token}/layout`

Applies one preset action:

```json
{ "action": "auto_orient" }
```

Allowed actions:

- `auto_orient`
- `rotate_z_90`
- `rotate_z_minus_90`
- `center`
- `arrange`
- `reset`

Returns the same scene metadata shape as `/stl/import`.

### `POST /stl/{draft_token}/3mf`

Materializes the current draft model as a Bambu/Orca-style 3MF and returns a normal 3MF token that can be used with existing `/3mf/{token}/inspect` and `/slice/v2` flows.

Response:

```json
{
  "input_token": "normal-3mf-token",
  "draft_token": "string"
}
```

## orca-headless C++ Mode

Add a C++ subcommand for STL draft operations, separate from `slice`. It should share JSON protocol conventions with existing binary modes.

The C++ binary remains stateless. FastAPI owns the draft cache and passes explicit input/output paths to each binary invocation. A layout call rehydrates the current draft model from the cached draft artifact, applies one action, writes an updated artifact, and returns fresh scene metadata.

Responsibilities:

- Load STL via `Model::read_from_file`.
- Add/default instances as the GUI does for non-project geometry.
- Remove zero-volume objects if libslic3r exposes the same helper used by the GUI.
- Ground objects with `ensure_on_bed`.
- Apply optional center, auto-orient, and arrange operations.
- Compute object bounding boxes and transforms for gateway rendering.
- Rehydrate draft model state from an input path and write the updated state to an output path.
- Export the draft model as a 3MF when requested.

The arrange implementation should reuse the existing headless arrange helper style in `cpp/src/slice_mode.cpp`, which already mirrors GUI ArrangeJob behavior for copies.

## Gateway API

Gateway owns user-facing draft sessions and source mesh serving.

### `POST /api/stl-drafts`

Accepts the user STL and selected slicer context, stores the STL, calls `orcaslicer-headless /stl/import`, and returns a gateway draft ID plus scene metadata.

### `GET /api/stl-drafts/{id}/source.stl`

Streams the original STL bytes so the browser can load it with Three.js `STLLoader`.

### `POST /api/stl-drafts/{id}/layout`

Forwards preset layout actions to `orcaslicer-headless`, stores the updated scene metadata, and returns it to the browser.

### `POST /api/stl-drafts/{id}/slice-jobs`

Asks `orcaslicer-headless` to materialize the draft 3MF, then submits the returned 3MF through the existing slice-job pipeline.

## Gateway Web

Add a preview surface for STL imports:

- Install Three.js.
- Use `STLLoader` to render the stored source STL.
- Draw a simple bed plane and printable area outline from scene metadata.
- Apply the authoritative transform returned by `orcaslicer-headless`.
- Provide preset controls:
  - Auto-orient
  - Rotate 90 degrees
  - Rotate -90 degrees
  - Center
  - Arrange
  - Reset
- Disable final slicing until a preview scene is loaded.
- Show warnings returned by `orcaslicer-headless`, especially non-printable/out-of-bed geometry and arrange failures.

The browser may animate camera/orbit controls for inspection, but it does not author arbitrary transforms in v1.

## Data Flow

1. User uploads STL in gateway.
2. Gateway creates an STL draft session and forwards the STL to `orcaslicer-headless`.
3. `orcaslicer-headless` imports, grounds, optionally centers/arranges/auto-orients, and returns scene metadata.
4. Gateway web renders the original STL with the returned transform.
5. User applies preset controls as needed.
6. Gateway forwards each control to `orcaslicer-headless` and re-renders the returned scene.
7. User accepts preview.
8. Gateway asks `orcaslicer-headless` to materialize the draft as 3MF.
9. Gateway submits that 3MF through the existing slice-job path.

## Error Handling

- Invalid or empty STL returns `400 invalid_stl`.
- Geometry too large for the selected bed returns scene metadata with `printable=false` and a warning when preview is still possible; materializing/slicing should fail if the model remains unprintable.
- Arrange failure returns `409 arrange_failed` for layout action calls, keeping the previous valid scene active.
- Auto-orient failure returns `409 auto_orient_failed` and keeps the previous valid scene active.
- Unknown draft token returns `404 draft_unknown`.
- Draft expiration returns `410 draft_expired`; gateway should ask the user to re-import.

## Testing

orcaslicer-headless:

- Unit tests for API validation and draft-token lifecycle.
- C++/integration smoke test: small STL imports, centers on A1 mini bed, returns printable scene metadata.
- Auto-orient test: action changes rotation and keeps model grounded.
- Arrange test: action places model inside printable area.
- Failure test: oversized STL reports non-printable / arrange failure clearly.
- Materialize test: draft 3MF can be inspected and sliced through existing `/slice/v2`.

bambu-gateway:

- API tests for draft create, source download, layout action, materialize-to-slice-job.
- Web tests for STL upload flow and disabled/enabled final slice state.
- Renderer smoke test or Playwright check that the Three.js canvas renders nonblank for a known STL fixture.

## Open Questions

- Draft cache TTL and size limits should match existing 3MF token-cache constraints unless implementation shows STL drafts need a separate budget.
- If early implementation shows a process profile is unnecessary for some import-only operations, gateway may still pass its selected process profile uniformly so the draft scene and final slice use the same context.
- Thumbnail generation for STL drafts is not required for v1 because the browser renders live preview, but slice-job thumbnails continue to work after final slicing if the sliced output contains a thumbnail.
