# API Changes: Explicit Plate Type Selection

## Overview

The slicing API now supports an explicit `plate_type` input, similar to machine/process/filament selection.

This allows clients to choose the bed surface used during slicing instead of relying on implicit defaults from the 3MF or OrcaSlicer preset behavior.

## New Endpoint

### `GET /profiles/plate-types`

Returns the supported plate types:

```json
[
  {"value": "cool_plate", "label": "Cool Plate"},
  {"value": "engineering_plate", "label": "Engineering Plate"},
  {"value": "high_temp_plate", "label": "High Temp Plate"},
  {"value": "textured_pei_plate", "label": "Textured PEI Plate"},
  {"value": "textured_cool_plate", "label": "Textured Cool Plate"},
  {"value": "supertack_plate", "label": "Supertack Plate"}
]
```

## Updated Endpoints

### `POST /slice`
### `POST /slice-stream`

New optional multipart form field:

- `plate_type` (string, snake_case)

Allowed values are exactly the list returned by `GET /profiles/plate-types`.

If an invalid value is provided, the API returns `400` with an error message.

## Resolution Rules

1. If `plate_type` is provided in the request, it is used.
2. If `plate_type` is not provided and the input 3MF has `curr_bed_type`, that value is preserved.
3. If neither exists, OrcaSlicer default behavior applies.

## Example

```bash
curl -s -o sliced.3mf \
  -F "file=@model.3mf" \
  -F "machine_profile=GM020" \
  -F "process_profile=GP000" \
  -F "plate_type=textured_pei_plate" \
  -F 'filament_profiles=["GFSL99_02"]' \
  http://localhost:8000/slice
```
