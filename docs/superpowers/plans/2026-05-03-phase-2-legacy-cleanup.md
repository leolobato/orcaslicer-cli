# Phase 2: Retire legacy `/slice` route + Python parsers — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Delete the AppImage-era code paths that `/slice/v2` and `/3mf/inspect` made redundant. After this lands, `orcaslicer-cli` has exactly one slicing entry (`/slice/v2`), exactly one inspect path (`/3mf/inspect`), no Python re-implementations of libslic3r logic, and `test_api.sh` smoke-tests the surviving routes.

**Architecture:** Mechanical deletion. The legacy `/slice` and `/slice-stream` multipart routes in `app/main.py` are removed along with their helpers in `app/slicer.py` (`slice_3mf`, `slice_3mf_streaming`, `_sanitize_3mf`, `_CLAMP_RULES`, `_extract_declared_customizations`, `_extract_declared_machine_customizations`) and the standalone `app/normalize.py` module. The `_write_bbl_machine_full_shims` startup hook in `app/profiles.py` is also retired — `/slice/v2` stamps `printer_model_id` directly via the request envelope (commit `64313d3`).

**Tech Stack:** Python (FastAPI), pytest. No C++ changes needed.

**Pre-requisites confirmed:**
- `bambu-gateway` migrated to `/slice/v2` and `/3mf/inspect` (commit `f0f4810` on the gateway side; commit `7d1642e` for follow-up wiring).
- Phase 1 + 3 + 3b shipped on `feat/orca-headless`.

**Repo conventions to honor:**
- Subject ≤ 60 chars; body wrapped at 140; `-` bullets; code names in backticks.
- Python edits hot-reload via `./app:/app/app` mount; just `docker compose restart orcaslicer-cli`.

**Out of scope:**
- Phase 4 (README rewrite, CI fidelity test wiring, residual ~0.6% structural diff, vendor submodule pointer drift).

---

## Milestones

1. **BBL shim writer** (Task 1) — independent, low-risk delete.
2. **Legacy `/slice` route + helpers** (Tasks 2–4) — coupled removal, plus smoke-script update.
3. **Bump version + verify** (Task 5) — API revision bump, full pytest run.

Each milestone ends green: container builds, `pytest -q` is green, smoke script (against `/slice/v2`) succeeds.

---

## Files to be deleted

- `app/normalize.py` — Python re-implementation of `Preset::normalize`. Now done in C++ `slice_mode.cpp` step 3.
- `tests/test_normalize.py` — covers only the deleted module.
- `tests/test_machine_full_shims.py` — covers only the deleted shim writer.
- `tests/test_sanitize_3mf.py` — covers only `_sanitize_3mf` which goes with the legacy route.

## Files to be modified

- `app/main.py` — remove `@app.post("/slice")` and `@app.post("/slice-stream")` route handlers + their imports of legacy helpers.
- `app/slicer.py` — remove `slice_3mf`, `slice_3mf_streaming`, `_sanitize_3mf`, `_CLAMP_RULES`, `_extract_declared_customizations`, `_extract_declared_machine_customizations`, and any helpers exclusively used by them. Keep `materialize_profiles_for_binary` (Phase 1, used by `/slice/v2`) and `get_machine_model_id`.
- `app/profiles.py` — remove `_write_bbl_machine_full_shims` and its lifespan call.
- `app/config.py` — bump `API_REVISION`.
- `test_api.sh` — point smoke script at `/slice/v2` JSON body instead of legacy multipart.
- Any test file that imports a deleted symbol — delete or update the import.

---

## Milestone 1 — BBL shim writer

### Task 1: Remove `_write_bbl_machine_full_shims`

**Why:** The shim writer materialized `BBL/machine_full/{name}.json` files at startup so the AppImage CLI could read `model_id` and stamp `printer_model_id` on `slice_info.config`. `/slice/v2` doesn't need this — `app/slicer.py::materialize_profiles_for_binary` returns `printer_model_id` from `get_machine_model_id` and the C++ binary stamps it directly via the request envelope (`cpp/src/slice_mode.cpp` step 8). The shim files are now write-only.

**Files:**
- Modify: `app/profiles.py` (delete `_write_bbl_machine_full_shims` and the `_write_bbl_machine_full_shims()` call in `load_all_profiles`)
- Delete: `tests/test_machine_full_shims.py`

- [ ] **Step 1: Confirm zero callers outside the file**

```bash
grep -rn "_write_bbl_machine_full_shims" app/ tests/ scripts/
```

Expected: only `app/profiles.py` (definition + 1 self-call) and `tests/test_machine_full_shims.py`. If any other module imports it, that module migrates first.

- [ ] **Step 2: Remove the function and its lifespan call**

In `app/profiles.py`:

```bash
# Find the lines to remove
grep -n "_write_bbl_machine_full_shims\|machine_full" app/profiles.py
```

Delete:
- The whole `def _write_bbl_machine_full_shims() -> None:` block (around lines 714–766).
- The `_write_bbl_machine_full_shims()` invocation in `load_all_profiles` (around line 849).
- Any docstring fragments in other functions that reference the shims (e.g. `get_machine_model_id`'s comment about "Same lookup pattern that `_write_bbl_machine_full_shims` uses…" — keep the function but trim the dead reference).

The shim files in `${ORCA_RESOURCES_DIR}/profiles/BBL/machine_full/` already on disk in deployed containers stay where they are — leaving them is fine; they're harmless.

- [ ] **Step 3: Delete the test**

```bash
git rm tests/test_machine_full_shims.py
```

- [ ] **Step 4: Verify**

```bash
docker compose restart orcaslicer-cli && sleep 5
docker exec orcaslicer-cli-orcaslicer-cli-1 pytest tests/ -q --ignore=tests/integration 2>&1 | tail -5
```

Expected: same green count as before minus the deleted test count. The startup log in `docker logs orcaslicer-cli-orcaslicer-cli-1 | tail -10` no longer prints `Wrote N BBL machine_full shim(s)`.

- [ ] **Step 5: Smoke `/slice/v2` against fixture 01 to confirm `printer_model_id` still lands**

```bash
TOK=$(curl -s -X POST http://localhost:8000/3mf -F "file=@/Users/leolobato/Documents/Projetos/Personal/3d/bambu_workspace/_fixture/01/reference-benchy-orca-no-filament-custom-settings.3mf" | python3 -c "import json,sys; print(json.load(sys.stdin)['token'])")
RESP=$(curl -s -X POST http://localhost:8000/slice/v2 -H 'Content-Type: application/json' -d "{\"input_token\":\"$TOK\",\"machine_id\":\"GM020\",\"process_id\":\"GP000\",\"filament_settings_ids\":[\"GFSA00_02\"],\"recenter\":false}")
OUT=$(echo "$RESP" | python3 -c "import json,sys; print(json.load(sys.stdin)['output_token'])")
curl -s -o /tmp/sliced-m1.3mf "http://localhost:8000/3mf/$OUT"
unzip -p /tmp/sliced-m1.3mf 'Metadata/slice_info.config' | grep printer_model_id
```

Expected: `<metadata key="printer_model_id" value="N1"/>` — same as before deletion.

- [ ] **Step 6: Commit**

```bash
git add app/profiles.py tests/test_machine_full_shims.py
git commit -m "Drop BBL machine_full shim writer; v2 stamps via request"
```

---

## Milestone 2 — Legacy `/slice` route + helpers

### Task 2: Final caller audit

**Why:** Before the deletion in Task 3, prove nothing outside this repo depends on `/slice` (multipart) or `/slice-stream`. The bambu-gateway migration (`bambu-gateway` commit `f0f4810`) already moved off, but external callers (curl users, CI scripts, the iOS app's emergency fallback) might still hit the legacy path.

**Files:**
- Read-only audit; no commits this task.

- [ ] **Step 1: Grep both repos for caller signatures**

```bash
grep -rn '"/slice"\|"/slice-stream"\|/slice "' \
  /Users/leolobato/Documents/Projetos/Personal/3d/bambu_workspace/bambu-gateway \
  /Users/leolobato/Documents/Projetos/Personal/3d/bambu_workspace/bambu-gateway-ios \
  /Users/leolobato/Documents/Projetos/Personal/3d/bambu_workspace/orcaslicer-cli \
  | grep -v '/slice/v2\|/slice-stream/v2\|node_modules\|build/\|\.git/'
```

Allowed remaining hits: `app/main.py` (the route definitions we're about to delete), `test_api.sh` (we update it in Task 4), the plan/spec markdown files, any docstring mentioning "/slice" historically.

NOT allowed: any production code in `bambu-gateway/app/`, any iOS app source.

- [ ] **Step 2: Decide**

If only allowed hits remain, proceed to Task 3. If a real caller is found, STOP and escalate — that caller migrates first.

- [ ] **Step 3: No commit**

This task produces no diff. Note the audit result in the Task 3 commit message body.

---

### Task 3: Delete `/slice`, `/slice-stream`, slicer helpers, and `app/normalize.py`

**Why:** The whole AppImage-era pipeline. `/slice/v2` and `/slice-stream/v2` are the production entries; their dependencies (`materialize_profiles_for_binary`, `BinaryClient`) live separately and don't pull any of the deleted code.

**Files:**
- Modify: `app/main.py` — remove `slice_file` (`/slice`) and `slice_stream` (`/slice-stream`) handlers + the helpers they import
- Modify: `app/slicer.py` — remove `slice_3mf`, `slice_3mf_streaming`, `_sanitize_3mf`, `_CLAMP_RULES`, `_extract_declared_customizations`, `_extract_declared_machine_customizations`, plus any other helpers used only by these
- Delete: `app/normalize.py`
- Delete: `tests/test_normalize.py`
- Delete: `tests/test_sanitize_3mf.py`
- Modify: any test that imported `slice_3mf`, `_extract_declared_customizations`, etc. — delete or rewrite to use `/slice/v2` if the assertion has unique value

- [ ] **Step 1: Inventory the legacy route handlers**

```bash
grep -n '@app.post(\|@app.delete(\|@app.get(' app/main.py | grep -E '"\/slice"|"/slice-stream"' -B0 -A0
# Read each handler function in full
```

Confirm the exact `def slice_file(...)` and `def slice_stream(...)` signatures. They probably accept `file: UploadFile` plus form fields. Their bodies call `slice_3mf` / `slice_3mf_streaming`. Note line ranges.

- [ ] **Step 2: Identify `app/main.py` imports that become orphans**

After deleting the two handlers, these imports may have no remaining users (grep to confirm):

- `slice_3mf`, `slice_3mf_streaming` from `app.slicer`
- `parse_filament_profile_ids` (only used by legacy slice route)
- `_detect_file_type` (only used by legacy slice route — check)
- Anything else from `app/slicer.py` that's only referenced inside the deleted handlers

For each import that becomes unused, remove it. Keep `materialize_profiles_for_binary`, `PLATE_TYPE_API_TO_ORCA`, `SUPPORTED_PLATE_TYPES`, validators (used by `/slice/v2` JSON body validation).

- [ ] **Step 3: Delete the route handlers in `app/main.py`**

Delete the entire `@app.post("/slice", ...)` decorator + `async def slice_file(...)` body, and the `@app.post("/slice-stream", ...)` + `async def slice_stream(...)` body. Adjust imports per Step 2.

- [ ] **Step 4: Delete the helpers in `app/slicer.py`**

Functions to remove:
- `_CLAMP_RULES` (module-level dict)
- `_sanitize_3mf` (uses `_CLAMP_RULES`)
- `_extract_declared_customizations`
- `_extract_declared_machine_customizations`
- `slice_3mf`
- `slice_3mf_streaming`
- Any helper inside slicer.py used ONLY by these (grep each candidate to confirm zero remaining callers).

Keep:
- `materialize_profiles_for_binary` (used by `/slice/v2`)
- `PLATE_TYPE_API_TO_ORCA`, `SUPPORTED_PLATE_TYPES`, `VALID_BRIM_TYPES`, `VALID_INFILL_PATTERNS`, `VALID_SUPPORT_TYPES` (validators consumed by `/slice/v2` request validation)
- `ModelTooBigError`, `SlicingError` (exception types still used by remaining code paths)

After deletion, `app/slicer.py` should be MUCH shorter. A reasonable target: under 500 lines (was ~1700+).

- [ ] **Step 5: Delete the standalone normalize module**

```bash
git rm app/normalize.py
```

Confirm nothing imports it:

```bash
grep -rn "from app.normalize\|import normalize" app/ tests/ scripts/
```

Should return zero hits.

- [ ] **Step 6: Delete tests targeting the removed code**

```bash
git rm tests/test_normalize.py tests/test_sanitize_3mf.py
```

Then for any other test file, grep for imports of the deleted symbols:

```bash
for sym in _CLAMP_RULES _sanitize_3mf _extract_declared_customizations _extract_declared_machine_customizations slice_3mf slice_3mf_streaming; do
    echo "=== $sym ==="
    grep -rln "$sym" tests/
done
```

For each match: either delete the test file (if it's exclusively about deleted code) or surgically remove the import + affected tests (if the file mixes deleted-code tests with surviving-code tests).

- [ ] **Step 7: Run the suite**

```bash
docker compose restart orcaslicer-cli && sleep 5
docker exec orcaslicer-cli-orcaslicer-cli-1 pytest tests/ -q --ignore=tests/integration
```

Expected: green. Test count should drop by however many tests targeted deleted code (likely 30-100 tests). No failures from surviving tests.

- [ ] **Step 8: Smoke `/slice/v2`**

```bash
TOK=$(curl -s -X POST http://localhost:8000/3mf -F "file=@/Users/leolobato/Documents/Projetos/Personal/3d/bambu_workspace/_fixture/01/reference-benchy-orca-no-filament-custom-settings.3mf" | python3 -c "import json,sys; print(json.load(sys.stdin)['token'])")
curl -s -X POST http://localhost:8000/slice/v2 -H 'Content-Type: application/json' -d "{\"input_token\":\"$TOK\",\"machine_id\":\"GM020\",\"process_id\":\"GP000\",\"filament_settings_ids\":[\"GFSA00_02\"],\"recenter\":false}" | python3 -c "import json,sys; d=json.load(sys.stdin); e=d['estimate']; print(f'time={e[\"time_seconds\"]:.0f} weight={e[\"weight_g\"]:.2f}g')"
```

Expected: `time=~1828 weight=10.63g` — same as before.

- [ ] **Step 9: Confirm `/slice` returns 404**

```bash
curl -s -o /dev/null -w "%{http_code}\n" -X POST http://localhost:8000/slice
```

Expected: `404` (route not registered) or `405` (method not allowed). NOT 200.

- [ ] **Step 10: Run the integration suite**

```bash
pytest tests/integration/ -v
```

Expected: same green count as before (5 tests).

- [ ] **Step 11: Commit**

Make ONE focused commit so the deletion is atomic and easy to revert:

```bash
git add app/main.py app/slicer.py app/profiles.py
git rm app/normalize.py tests/test_normalize.py tests/test_sanitize_3mf.py
# Plus any other test files removed in Step 6
git commit -m "$(cat <<'EOF'
Retire legacy /slice multipart route and Python parsers

Removes the AppImage-era pipeline that `/slice/v2` and `/3mf/inspect`
made redundant. After this commit:

- `/slice` and `/slice-stream` (multipart) — gone. `/slice/v2` and
  `/slice-stream/v2` are the production entries.
- `slice_3mf`, `slice_3mf_streaming`, `_sanitize_3mf`, `_CLAMP_RULES`,
  `_extract_declared_customizations`, `_extract_declared_machine_customizations`
  in `app/slicer.py` — gone. The C++ binary owns these via
  `apply_overrides_for_slot` and libslic3r's native validators.
- `app/normalize.py` — gone. `Preset::normalize` runs natively in
  `cpp/src/slice_mode.cpp` step 3.

bambu-gateway already migrated to the v2 endpoints (gateway commits
`f0f4810` + `7d1642e`). Caller audit confirmed no other callers remained.
EOF
)"
```

---

### Task 4: Update `test_api.sh` smoke script

**Why:** The smoke script hits `/slice` (multipart) which now returns 404. Re-target at `/slice/v2` (JSON body) so the script is a meaningful production smoke.

**Files:**
- Modify: `test_api.sh`

- [ ] **Step 1: Read the current shape**

```bash
sed -n '70,160p' test_api.sh
```

Identify the three slice tests at lines 85, 117, 147. Each builds a multipart body and POSTs to `/slice`.

- [ ] **Step 2: Rewrite to use `/slice/v2`**

The new flow per slice test:

```bash
# Upload first to get a token
TOK=$(curl -s -X POST "$BASE_URL/3mf" \
    -F "file=@$EXAMPLES_DIR/example3.3mf" \
  | python3 -c "import json,sys; print(json.load(sys.stdin)['token'])")

# Slice via v2 JSON body
HTTP_CODE=$(curl -s -o "$SLICE_OUT_RAW" -w "%{http_code}" \
    -X POST "$BASE_URL/slice/v2" \
    -H 'Content-Type: application/json' \
    -d "{
        \"input_token\": \"$TOK\",
        \"machine_id\": \"GM020\",
        \"process_id\": \"GP000\",
        \"filament_settings_ids\": [\"GFSL99_02\"],
        \"recenter\": false
    }")

# v2 returns JSON (not 3MF bytes); follow the download_url for the actual 3MF
OUT_TOKEN=$(python3 -c "import json,sys; print(json.load(open('$SLICE_OUT_RAW'))['output_token'])")
curl -s -o "$SLICE_OUT" "$BASE_URL/3mf/$OUT_TOKEN"
```

Then the existing assertion `output is non-empty ($SIZE bytes)` runs against `$SLICE_OUT` as before, and the gcode-presence check works on the downloaded 3MF.

The legacy path's `plate_type=textured_pei_plate` form field becomes `curr_bed_type` carried by the input 3MF; for the smoke test that's already correct (the example 3MF carries its own bed type).

- [ ] **Step 3: Verify**

Run the script against the local container:

```bash
./test_api.sh
```

Expected: all checks pass (or the same pre-existing failures the script had before).

- [ ] **Step 4: Commit**

```bash
git add test_api.sh
git commit -m "Smoke /slice/v2 + token cache instead of legacy /slice"
```

---

## Milestone 3 — Bump version, final verification

### Task 5: Bump API revision and run full suite

**Why:** Phase 2 is a breaking removal (the `/slice` route no longer exists). Bump `API_REVISION` so deployed containers report the new version.

**Files:**
- Modify: `app/config.py` — bump `API_REVISION` (e.g. `2.3.2-19` → `2.3.2-20`).

- [ ] **Step 1: Find and bump the revision**

```bash
grep -n "API_REVISION" app/config.py
```

Bump the patch suffix by one.

- [ ] **Step 2: Run the full pytest suite + integration tests**

```bash
docker compose restart orcaslicer-cli && sleep 5
docker exec orcaslicer-cli-orcaslicer-cli-1 pytest tests/ -q --ignore=tests/integration
pytest tests/integration/ -v
```

Both green.

- [ ] **Step 3: Verify the version log**

```bash
docker logs orcaslicer-cli-orcaslicer-cli-1 2>&1 | grep "orcaslicer-cli " | tail -1
curl -s http://localhost:8000/health
```

Expected: new version string in both.

- [ ] **Step 4: Commit**

```bash
git add app/config.py
git commit -m "Bump API revision: legacy /slice retired"
```

---

## Self-Review

**Spec coverage** (vs the Phase 2 roadmap line-items in earlier plans):

- "Delete `app/normalize.py`" — Task 3.
- "Delete `_CLAMP_RULES` in `app/slicer.py`" — Task 3.
- "Delete the smart settings transfer block (`_extract_declared_customizations` and friends)" — Task 3.
- "Delete the BBL `machine_full` shim writer" — Task 1.
- "Retire the legacy `/slice` route" — Task 3.

**Risks:**

- **Hidden caller** of `/slice` outside the two repos audited in Task 2. Mitigation: Task 2's audit grep + an extra search across `~/.zsh_history`-style scripts the user maintains. If a caller surfaces post-deletion, revert the focused commit in Task 3 — clean rollback because the deletion is atomic.
- **Test files mixing deleted-code tests with surviving-code tests.** Task 3 step 6 calls this out — surgical delete, not file-level delete, when the file has mixed concerns.
- **`parse_filament_profile_ids` and `_detect_file_type` orphan check.** These imports might be referenced from `/slice/v2` too (multi-purpose helpers). Task 3 step 2 covers — grep before removing the import lines.

**Placeholder scan:** every step has an actual command, code block, or grep to run. No "TBD" / "similar to Task N" / "add error handling" patterns.
