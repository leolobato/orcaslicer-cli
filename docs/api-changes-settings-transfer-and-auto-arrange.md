# API Changes: Smart Settings Transfer & Auto-Arrange

## Overview

Two new behaviors when slicing 3MF files with different profiles than the ones they were saved with:

1. **Smart Settings Transfer** — Only user customizations from the 3MF are carried over to the target profile, instead of blindly overwriting all settings.
2. **Auto-Arrange** — Models positioned for a larger printer bed are automatically repositioned when slicing for a smaller printer, instead of being rejected.

---

## Smart Settings Transfer

### Problem

When a user saves a 3MF in OrcaSlicer with a process profile (e.g., "0.20mm Standard @BBL X1C") and customizes some settings (e.g., changes infill pattern to Gyroid), those customizations are embedded in the file alongside hundreds of default profile values.

Previously, the API blindly overlaid **all** embedded settings onto whatever process profile the client requested. This meant the target profile's defaults were mostly overwritten by the source profile's defaults — defeating the purpose of choosing a different profile.

Now, the API detects which settings the user actually customized and transfers only those.

### New Response Headers

Every `/slice` response now includes:

#### `X-Settings-Transfer-Status`

Always present. One of:

| Value | Meaning |
|-------|---------|
| `applied` | User customizations were detected and transferred to the target profile. |
| `no_customizations` | The 3MF settings match the original profile exactly — no transfer needed. |
| `no_original_profile` | The original profile referenced in the 3MF wasn't found in our profile library. Fell back to full overlay (previous behavior). |
| `no_3mf_settings` | The 3MF has no embedded process settings or no `print_settings_id`. Fell back to full overlay (previous behavior). |

#### `X-Settings-Transferred`

Present only when status is `applied`. A JSON array of objects describing each transferred setting:

```json
[
  {
    "key": "sparse_infill_pattern",
    "value": "gyroid",
    "original": "crosshatch"
  },
  {
    "key": "sparse_infill_density",
    "value": "25%",
    "original": "15%"
  }
]
```

- `key` — The OrcaSlicer setting name.
- `value` — The user's customized value (from the 3MF).
- `original` — The default value from the 3MF's original profile (`None` if the setting didn't exist in the original profile).

### Suggested UX

| Status | Suggested User Feedback |
|--------|------------------------|
| `applied` | Show a summary: *"Transferred N custom setting(s) from your file: [list keys]."* Optionally display the before/after values. |
| `no_customizations` | Silent or brief: *"Sliced with [profile name]. No custom settings detected in file."* |
| `no_original_profile` | Warning: *"Could not identify the original profile in your file. All embedded settings were applied as-is."* |
| `no_3mf_settings` | Silent — the file had no embedded process settings, which is normal for freshly exported STL→3MF files. |

---

## Auto-Arrange for Cross-Printer Slicing

### Problem

When a 3MF is saved for a larger printer (e.g., P1S with a 256×256mm bed), the model's position on the bed is embedded in the file. If the client requests slicing for a smaller printer (e.g., A1 mini with 180×180mm bed), the model's coordinates may be outside the smaller bed — even though the model itself fits.

Previously, this was rejected with a `400` error.

### New Behavior

The validation now distinguishes two cases:

1. **Model too large** — The model's physical dimensions exceed the target bed. Still returns `400` with an error message (unchanged).
2. **Model fits but is off-plate** — The model dimensions fit, but its position is outside the target bed bounds. The API automatically passes `--arrange 1` to OrcaSlicer, which repositions the model on the target bed. The slice succeeds with `200`.

### Client Impact

- Requests that previously returned `400` for cross-printer slicing will now succeed with `200` when the model physically fits.
- No changes needed on the client side — this is transparent.
- If you were showing the "model too big" error to users, those errors now only appear when the model genuinely doesn't fit.

### Suggested UX

Since the auto-arrange is transparent, no special feedback is required. However, if you want to inform the user:

- You can detect this happened by comparing the requested machine profile against the `print_settings_id` from the original 3MF (if you have access to it).
- Alternatively, check the server logs — the API logs `"Adding --arrange 1 (model position is off-plate for target printer)"` when this kicks in.

---

## Example Request & Response

```bash
curl -s -D headers.txt -o sliced.3mf \
  -F "file=@model.3mf" \
  -F "machine_profile=GM020" \
  -F "process_profile=GP000" \
  -F 'filament_profiles=["GFSL99_02"]' \
  http://localhost:8000/slice

cat headers.txt
# HTTP/1.1 200 OK
# x-settings-transfer-status: applied
# x-settings-transferred: [{"key": "sparse_infill_pattern", "value": "gyroid", "original": "crosshatch"}]
# content-disposition: attachment; filename=sliced.3mf
```

## Backward Compatibility

- The `/slice` endpoint still returns `200` with the sliced 3MF binary on success.
- The new headers are additive — clients that don't read them are unaffected.
- The only behavioral change is that some requests that previously returned `400` (model off-plate for smaller printer) now succeed.
