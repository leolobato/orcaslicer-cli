# Phase 5: GUI-equivalent profile resolution on machine switch — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Mirror OrcaSlicer GUI's behaviour when the user picks a different machine for a 3MF authored for another printer: the GUI silently substitutes incompatible filament/process presets with the same-alias variant for the new machine, and resets `curr_bed_type` to the new printer's default if its current value is unsupported. Today our gateway carries the 3MF's authored values straight through, which trips the slicer's compat check (`IncompatibleFilamentError`) for "unused" filament slots that the UI hides from the user. After this lands, the gateway calls a single slicer-side resolver when the user picks a machine, populates the form with GUI-equivalent defaults, and the entire class of slot-1-mismatch failures disappears at the source.

**Architecture:** New endpoint `POST /profiles/resolve-for-machine` on `orcaslicer-cli`. Takes the target machine + the 3MF's authored process/filament/plate-type values; returns GUI-equivalent replacements with a per-field `match` reason (`alias` / `default` / `first_compat` / `unchanged` / etc.). Implementation mirrors `PresetBundle::update_compatible` from `vendor/OrcaSlicer/src/libslic3r/PresetBundle.cpp:4358`, scoring filament candidates with the alias/type/visibility logic from `PreferedFilamentProfileMatch` (`PresetBundle.cpp:4416-4441`), process candidates with `PreferedPrintProfileMatch` (`PresetBundle.cpp:4388-4411`), and bed-type using each machine_model's `not_support_bed_type` + `default_bed_type` (`Bambu Lab A1 mini.json` example: `"not_support_bed_type": "Smooth Cool Plate;Engineering Plate"`, `"default_bed_type": "Textured PEI Plate"`). The gateway calls the endpoint on machine selection and hydrates the print form with the resolved values; the user can override anything before submitting.

**Tech Stack:** Python 3.12 (FastAPI, Pydantic) on the slicer side. Python 3.12 + React/TypeScript (TanStack Query) on the gateway side. Pytest both sides.

**Where this plan lives, where it executes:** Plan committed to `orcaslicer-cli/docs/superpowers/plans/`. Tasks 1–3 execute in `orcaslicer-cli/`. Tasks 4–6 execute in `bambu-gateway/`. Branch on both repos: `feat/orca-headless`.

**Repo conventions to honour:**
- Slicer side: profile lookup goes through `app/profiles.py` (don't reach into `_raw_profiles` from `app/main.py`). New helpers live in `app/profiles.py`. New endpoint goes in `app/main.py` next to the existing `/profiles/*` routes. Pydantic response models in `app/models.py`.
- Gateway side: `SlicerClient` (`app/slicer_client.py`) is the only thing that talks to the slicer. The print form lives in `web/src/routes/print.tsx`; auto-match logic in `web/src/lib/api/`.
- Commits: subject ≤ 60, body wrapped at 140, code names in backticks.

**Out of scope:**
- iOS app changes. The gateway's API shape stays identical for `/api/slice-jobs` etc.; only the form pre-fill changes.
- Frontend redesign. The plate-type and process dropdowns already exist; we just hydrate them with the resolver's output.
- Removing user-editable overrides. The user must still be able to pick a different filament/process/plate type after the resolver suggests defaults.
- AMS auto-match. Existing `/api/filament-matches` continues to map resolved filaments → AMS trays; this plan doesn't touch it.

---

## Milestones

1. **Slicer endpoint built** (Tasks 1–3) — filament + process + plate-type resolution behind a single endpoint, with unit tests covering each.
2. **Gateway integration** (Tasks 4–5) — `SlicerClient.resolve_for_machine`, `print.tsx` calls it on machine change and pre-fills form fields.
3. **Cleanup** (Task 6) — remove the slot-0 fallback hack in `_normalize_filament_selection`; the resolver makes it redundant.

Each milestone ends green: full pytest passes, smoke against a multi-filament cross-machine 3MF (e.g. the P2S-authored Singlecolor benchy on A1 mini) succeeds end-to-end.

---

## Files to be created or modified

**Slicer (`orcaslicer-cli/`):**
- `app/profiles.py` — add `resolve_filament_for_machine`, `resolve_process_for_machine`, `resolve_plate_type_for_machine`. Expose `get_machine_model_metadata(slug) -> {default_bed_type, not_support_bed_types, default_materials, default_filament_profile, default_print_profile}` so the resolver and any future caller share one source of truth.
- `app/main.py` — new `POST /profiles/resolve-for-machine` route.
- `app/models.py` — new request + response Pydantic models.
- `tests/test_resolve_for_machine.py` — unit tests for each resolver.
- `test_api.sh` — extend smoke to hit the new endpoint with a known cross-machine fixture.

**Gateway (`bambu-gateway/`):**
- `app/slicer_client.py` — new `SlicerClient.resolve_for_machine(...)` method.
- `app/main.py` — proxy endpoint `POST /api/slicer/resolve-for-machine` that forwards to the slicer.
- `web/src/lib/api/slicer-profiles.ts` — `resolveForMachine(...)` helper.
- `web/src/routes/print.tsx` — call the resolver when `settings.machine` changes (after the existing name→setting_id translation), update `settings.process`, `settings.plateType`, and seed `filamentMapping` from the resolved filament list.
- `web/src/lib/api/types.ts` — TS types for the resolver request/response.
- `tests/test_slicer_client_resolve.py` — `httpx.MockTransport` test pinning the wire shape.
- `tests/integration/test_resolve_for_machine_live.py` — opt-in live test.

**Removed (after Task 6):**
- The `if overridden: fallback = filament_ids[min(overridden)] ...` block in `bambu-gateway/app/slicer_client.py::_normalize_filament_selection`.

---

## Milestone 1 — Slicer endpoint

### Task 1: `app/profiles.py` — resolution helpers

**Why:** Encapsulate the GUI-equivalent matching logic in one place, on top of the existing `_raw_profiles` index. Keeps the FastAPI route in `main.py` thin and lets unit tests target the matching logic directly.

**Files:**
- Modify: `orcaslicer-cli/app/profiles.py`

- [ ] **Step 1: Expose machine_model metadata**

The `machine_model` JSONs (e.g. `BBL/machine/Bambu Lab A1 mini.json`) carry `not_support_bed_type` (semicolon-separated, singular in JSON despite the C++ name being plural — see `vendor/OrcaSlicer/src/libslic3r/PresetBundle.cpp:3952`), `default_bed_type`, `default_materials`. Add:

```python
def get_machine_model_metadata(slug: str) -> dict[str, Any]:
    """Return the machine_model fields the GUI uses for compat fallback.

    Returns:
      - "name": the printer_model display name (e.g. "Bambu Lab A1 mini")
      - "default_bed_type": e.g. "Textured PEI Plate", or "" if absent
      - "not_support_bed_types": list[str] (split from semicolon-joined source)
      - "default_filament_profile": list[str] (from the machine variant)
      - "default_print_profile": str (from the machine variant)
      - "default_materials": list[str] (from the machine_model JSON)
    """
```

Look up via the same pattern `get_machine_model_id` uses (`app/profiles.py:1295`): resolve the variant by slug, read `printer_model`, then walk `_raw_profiles` for a top-level `machine_model` whose `name` matches. `default_filament_profile` / `default_print_profile` live on the variant; `not_support_bed_type`, `default_bed_type`, `default_materials` live on the machine_model.

- [ ] **Step 2: Add `_score_filament_for_machine`**

Mirror `PreferedFilamentProfileMatch::operator()` (`vendor/OrcaSlicer/src/libslic3r/PresetBundle.cpp:4416-4441`):

```python
def _score_filament_for_machine(
    candidate: dict,
    *,
    requested_alias: str,         # alias of the user's currently-selected filament
    requested_type: str,          # e.g. "PLA" — from filament_type[0]
    prefered_names: list[str],    # printer's default_filament_profile
) -> int:
    """Higher = better. Returns 0 for default/external presets.

    Mirrors GUI's PreferedFilamentProfileMatch:
      - alias match wins outright (returns int.max)
      - +1 if name in prefered_names
      - +1 if visible (we treat all loaded presets as visible — pre-filtering is a Phase 6 concern)
      - ×10 if filament_type matches
    """
```

The alias is derived from the existing `_logical_filament_name(name)` helper (`app/profiles.py:147`), which strips the ` @<printer>` suffix.

- [ ] **Step 3: Add `resolve_filament_for_machine`**

```python
def resolve_filament_for_machine(
    machine_slug: str,
    requested_filament_name: str,
) -> dict[str, Any]:
    """Find the best replacement filament for `machine_slug`, GUI-style.

    Returns:
      - "setting_id": resolved leaf's setting_id, or "" if no compat
      - "name": resolved leaf's display name
      - "match": "unchanged" | "alias" | "type" | "default" | "first_compat" | "none"
      - "alias": the canonical alias (`Bambu PLA Basic`, etc.)
    """
```

Algorithm:
1. Resolve the machine variant; collect `compatible_printers` candidates by listing every `instantiation: "true"` filament whose `compatible_printers` contains the machine variant's `name`.
2. If `requested_filament_name` is itself in the candidate set, return `match="unchanged"`.
3. Otherwise score each candidate with `_score_filament_for_machine`; sort desc; return the top one with the appropriate `match` reason.
4. If no candidates, return `match="none"` with empty fields — the caller surfaces this as a 422 or warning.

- [ ] **Step 4: Add `resolve_process_for_machine`**

Same shape as filament resolution, mirroring `PreferedPrintProfileMatch` (`PresetBundle.cpp:4388-4411`). Score by:
- alias match wins outright
- +1 if name == `default_print_profile`
- +1 if visible
- ×10 if `layer_height` is within 0.0005 of the requested process's layer_height

- [ ] **Step 5: Add `resolve_plate_type_for_machine`**

```python
def resolve_plate_type_for_machine(
    machine_slug: str,
    requested_plate_type: str,   # API value, e.g. "supertack_plate"
) -> dict[str, Any]:
    """If the requested plate type isn't supported, fall back to the machine's
    default_bed_type. No clever matching — mirrors PartPlateList::check_all_plate_local_bed_type
    (vendor/OrcaSlicer/src/slic3r/GUI/PartPlate.cpp:4524) which just resets to btDefault.
    """
```

Map API values to OrcaSlicer labels via the existing `PLATE_TYPE_API_TO_ORCA` (`app/slicer.py:18-25`); reverse-map at the boundary. Returns `match="unchanged"` or `match="default"` (or `match="none"` if the machine has no `default_bed_type` declared, which is rare).

- [ ] **Step 6: Unit tests**

Add `tests/test_resolve_for_machine.py` with at least:
- Filament: alias match (`Bambu PLA Basic @BBL P2S` → `Bambu PLA Basic @BBL A1M` on A1 mini)
- Filament: type fallback when no alias match (use a pair where alias differs but type matches)
- Filament: `match="unchanged"` when target is already compat
- Process: alias match (`0.20mm Standard @BBL P2S` → `0.20mm Standard @BBL A1M`)
- Process: layer_height proximity (`0.20mm Standard` mismatch but same height)
- Plate type: unsupported → default (use a machine with `not_support_bed_type` populated; A1 mini lists `Smooth Cool Plate;Engineering Plate`)
- Plate type: supported → unchanged

- [ ] **Step 7: Commit**

```bash
git add app/profiles.py tests/test_resolve_for_machine.py
git commit -m "Add GUI-equivalent profile resolution helpers"
```

---

### Task 2: `app/main.py` — `POST /profiles/resolve-for-machine`

**Files:**
- Modify: `orcaslicer-cli/app/main.py`, `app/models.py`

- [ ] **Step 1: Pydantic models in `app/models.py`**

```python
class ResolveForMachineRequest(BaseModel):
    machine_id: str
    process_id: str = ""
    process_name: str = ""        # one of the two is required; resolver tolerates either
    filament_names: list[str] = []
    plate_type: str = ""          # API value, e.g. "supertack_plate"

class ResolvedFilament(BaseModel):
    slot: int
    requested: str
    setting_id: str
    name: str
    match: Literal["unchanged", "alias", "type", "default", "first_compat", "none"]

class ResolvedProcess(BaseModel):
    requested: str
    setting_id: str
    name: str
    match: Literal["unchanged", "alias", "layer_height", "default", "first_compat", "none"]

class ResolvedPlateType(BaseModel):
    requested: str
    resolved: str
    match: Literal["unchanged", "default", "none"]

class ResolveForMachineResponse(BaseModel):
    machine_id: str
    machine_name: str
    process: ResolvedProcess | None = None
    filaments: list[ResolvedFilament] = []
    plate_type: ResolvedPlateType | None = None
```

- [ ] **Step 2: Route handler**

```python
@app.post("/profiles/resolve-for-machine", response_model=ResolveForMachineResponse, tags=["Profiles"])
def resolve_for_machine(req: ResolveForMachineRequest) -> ResolveForMachineResponse:
    """Return GUI-equivalent compat replacements for the given machine."""
```

- 404 if `machine_id` doesn't resolve to a machine variant (`get_profile("machine", req.machine_id)` raises) — return `{"code": "machine_unknown", "machine_id": req.machine_id}`.
- For each `filament_names[i]`, call `resolve_filament_for_machine` and emit a `ResolvedFilament` with `slot=i`.
- For `process_id` / `process_name` (whichever is set), call `resolve_process_for_machine`.
- For `plate_type`, call `resolve_plate_type_for_machine`. Skip if blank.

- [ ] **Step 3: Smoke**

```bash
TOK=$(curl -s -X POST http://10.0.1.9:8000/3mf -F "file=@<P2S 3mf>" | python3 -c "import json,sys; print(json.load(sys.stdin)['token'])")
curl -s -X POST http://10.0.1.9:8000/profiles/resolve-for-machine \
  -H 'Content-Type: application/json' \
  -d '{"machine_id":"GM020","process_name":"0.20mm Standard @BBL P2S","filament_names":["Bambu PLA Basic @BBL P2S","Bambu PLA Basic @BBL P2S"],"plate_type":"supertack_plate"}' \
  | python3 -m json.tool
```

Expected:
- `process.resolved_setting_id` = the A1M variant of the same alias
- both filaments resolved to `Bambu PLA Basic @BBL A1M`
- `plate_type` either `unchanged` (Supertack supported) or `default` with the A1 mini's default

- [ ] **Step 4: Commit**

```bash
git add app/main.py app/models.py
git commit -m "POST /profiles/resolve-for-machine returns GUI-equivalent presets"
```

---

### Task 3: Bump `API_REVISION` and update README

- [ ] **Step 1:** `app/config.py` — bump `API_REVISION` (e.g. `"23"` → `"24"`).
- [ ] **Step 2:** `README.md` — add a brief section for the new endpoint under the existing "Endpoints" list, including the request shape.
- [ ] **Step 3:** Commit:

```bash
git add app/config.py README.md
git commit -m "Bump API revision; doc /profiles/resolve-for-machine"
```

---

## Milestone 2 — Gateway integration

### Task 4: `SlicerClient.resolve_for_machine` + tests

**Files:**
- Modify: `bambu-gateway/app/slicer_client.py`
- Create: `bambu-gateway/tests/test_slicer_client_resolve.py`

- [ ] **Step 1: Method**

```python
async def resolve_for_machine(
    self,
    *,
    machine_id: str,
    process_id: str = "",
    process_name: str = "",
    filament_names: list[str] | None = None,
    plate_type: str = "",
) -> dict:
    """POST /profiles/resolve-for-machine. Returns the JSON response as-is."""
```

Use `httpx.AsyncClient(transport=self._transport)` for testability. Map non-200 to `SlicingError(...)` with the response body in the message.

- [ ] **Step 2: `httpx.MockTransport` test** pinning request shape and response parsing.

- [ ] **Step 3:** Commit:

```bash
git add app/slicer_client.py tests/test_slicer_client_resolve.py
git commit -m "Wire SlicerClient.resolve_for_machine to slicer"
```

---

### Task 5: Frontend — call resolver on machine change, pre-fill form

**Files:**
- Modify: `bambu-gateway/app/main.py` — add `POST /api/slicer/resolve-for-machine` proxy.
- Modify: `bambu-gateway/web/src/lib/api/slicer-profiles.ts` — `resolveForMachine` helper.
- Modify: `bambu-gateway/web/src/lib/api/types.ts` — TS types matching the slicer response.
- Modify: `bambu-gateway/web/src/routes/print.tsx`.

- [ ] **Step 1: Gateway proxy** at `/api/slicer/resolve-for-machine` that forwards JSON to `slicer_client.resolve_for_machine`. No business logic.

- [ ] **Step 2: TS API helper** mirroring the slicer's request/response shape.

- [ ] **Step 3: Wire into `print.tsx`**

After the existing `useEffect` that translates `settings.machine` from name → setting_id (around `print.tsx:113-121`), add another effect that fires when:
- `settings.machine` is a known setting_id (the `machineIsSettingId` flag I added for `processesQuery`), AND
- `state.info` is loaded (so we have the 3MF's authored values), AND
- the user hasn't manually overridden the targets yet.

That effect calls `resolveForMachine({machine_id, process_name, filament_names: info.filaments.map(f => f.setting_id), plate_type})` and on response:
- Updates `settings.process` with `resolved.process.setting_id` if `match !== "unchanged"`.
- Updates `settings.plateType` similarly.
- Updates `filamentMapping` so the AMS-tray dropdowns are seeded with the resolved filament names (the `setting_id` is what gets sent to the slicer; the display name flows through to the auto-match path).

Surface a small toast or banner ("Adjusted for {machine_name}: process X → Y, filaments swapped to A1M variants") so the user knows what changed and isn't surprised. The banner clears once they explicitly accept (e.g. on first explicit edit) — keep this simple, don't over-engineer.

- [ ] **Step 4: Smoke in browser** — load the P2S Singlecolor benchy, switch machine to A1 mini, confirm the form fields rotate to A1M variants and slicing succeeds.

- [ ] **Step 5:** Commit:

```bash
git add app/main.py web/src/lib/api/ web/src/routes/print.tsx
git commit -m "Hydrate print form via /profiles/resolve-for-machine on machine switch"
```

---

## Milestone 3 — Remove the gateway hack

### Task 6: Drop the slot-0 fallback in `_normalize_filament_selection`

**Why:** With the resolver running on machine change, the form's filament list always reflects machine-compat values; the dict-form payload submitted to the slicer will never contain incompat filaments for unused slots. The "replicate first override" code in `slicer_client._normalize_filament_selection` becomes redundant.

**Files:**
- Modify: `bambu-gateway/app/slicer_client.py`

- [ ] **Step 1: Delete the block**

Remove the `if overridden: fallback = ...` block (added in commit `<sha-of-the-stopgap>`); keep the `overridden` set tracking only if it stays useful for other logic (it doesn't — drop it too).

- [ ] **Step 2: Add a regression test**

`tests/test_slicer_client_normalize.py` (new): with a 3MF declaring 2 P2S filaments and a sparse dict overriding only slot 0 to an A1M filament, assert that `_normalize_filament_selection` now returns `["A1M_filament", "Bambu PLA Basic @BBL P2S"]` (i.e. the authored slot 1 stays untouched). This test pins the expectation that the resolver upstream is responsible for compat — the slicer_client doesn't second-guess.

- [ ] **Step 3: Run the gateway test suite**

```bash
pytest -q
```

Expected: green. The earlier slot-0 fallback test (added with the stopgap, if any) should be replaced by this one.

- [ ] **Step 4: Commit:**

```bash
git add app/slicer_client.py tests/test_slicer_client_normalize.py
git commit -m "Drop slot-0 fallback; resolver handles cross-machine compat"
```

---

## Self-Review

**Spec coverage** vs the original question ("how does the GUI handle machine switch for filaments / process / plate type"):
- Filament: alias > default_filament_profile > filament_type, mirroring `PreferedFilamentProfileMatch` (`PresetBundle.cpp:4416-4441`). ✅
- Process: alias > default_print_profile > layer_height proximity, mirroring `PreferedPrintProfileMatch` (`PresetBundle.cpp:4388-4411`). ✅
- Plate type: unsupported → machine's `default_bed_type`, mirroring `PartPlateList::check_all_plate_local_bed_type` (`PartPlate.cpp:4524`). ✅

**Open follow-ups called out inline:**
- Visible-preset filtering: the GUI bumps score for visible presets. Our slicer treats all loaded presets as visible (no UI). Phase 6 if anyone wants that behaviour ported.
- AMS auto-match: continues to run on the resolved filament list rather than the authored one. The existing `_build_project_filament_matches` logic already handles whatever it's given; no changes needed here, but worth a smoke test once the resolver lands.
- Banner UX: keep the "Adjusted for {machine_name}" surface minimal in this plan; richer UX (per-field diff, "revert to authored" button) is its own ticket.

**Placeholder scan:** every step has either code or a concrete command. No "TBD".

**Risks:**
- Task 5 frontend wiring needs to avoid an infinite loop: after the resolver updates `settings.process`, the next render must NOT re-run the resolver because it ran once for this machine. Track resolution per-machine_id in a ref or via a dedicated flag (`resolvedForMachine: string | null`) so we only resolve once per `(machine_id, info)` pair. The user's manual edits afterward must not re-trigger.
- If the slicer returns `match: "none"` for any field, surface clearly to the user instead of silently leaving the form blank — the request will fail validation later anyway.
- The resolver runs synchronously in the slicer (no I/O outside the in-memory profile catalog) so latency is sub-millisecond; the gateway's existing `staleTime: Infinity` query caching strategy applies cleanly.
