"""FastAPI app exposing OrcaSlicer as a REST API."""

import io
import json
import zipfile
from datetime import datetime, timezone
import logging
import os
from contextlib import asynccontextmanager
from typing import Any

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO").upper())
logger = logging.getLogger(__name__)


class _DropSuccessfulGetAccessLog(logging.Filter):
    """Drop uvicorn access-log lines for successful GET requests.

    The dashboard polls a handful of read endpoints every few seconds and
    drowns the slicer logs in noise; we keep POST/PUT/DELETE and any
    non-2xx response so real activity stays visible.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        # uvicorn.access logs as: '%s - "%s %s HTTP/%s" %d'
        # → args is (client_addr, method, path, http_version, status_code)
        args = record.args
        if not isinstance(args, tuple) or len(args) < 5:
            return True
        method = args[1]
        status = args[4]
        try:
            status_code = int(status)
        except (TypeError, ValueError):
            return True
        return not (
            isinstance(method, str)
            and method.upper() == "GET"
            and 200 <= status_code < 300
        )


logging.getLogger("uvicorn.access").addFilter(_DropSuccessfulGetAccessLog())

from fastapi import FastAPI, File, Query, Request, UploadFile, status as fastapi_status
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from starlette.responses import StreamingResponse

from .cache import TokenCache
from . import config as cfg
from .config import GIT_COMMIT, USER_PROFILES_DIR, VERSION
from .models import (
    FilamentProfile,
    FilamentProfileDeleteResponse,
    FilamentProfileImportPreview,
    FilamentProfileImportResponse,
    HealthResponse,
    MachineProfile,
    PlateTypeOption,
    ProcessProfile,
    ProcessProfileDeleteResponse,
    ProcessProfileImportPreview,
    ProcessProfileImportResponse,
    ReloadResponse,
    SliceError,
)
from .profiles import (
    ProfileNotFoundError,
    UnresolvedChainError,
    _filament_alias,
    _resolve_chain_for_payload,
    _safe_filename,
    export_user_filament,
    get_filament_profiles,
    get_machine_profiles,
    get_process_profiles,
    get_profile,
    get_profile_detail,
    load_all_profiles,
    materialize_filament_import,
    materialize_process_import,
)
from .inspect import (
    INSPECT_SCHEMA_VERSION, InspectCache, parse_inspect_data,
)
from .threemf import list_plate_thumbnails, read_plate_thumbnail
from .binary_client import BinaryClient, BinaryError
from .slicer import (
    PLATE_TYPE_API_TO_ORCA,
    SUPPORTED_PLATE_TYPES,
    VALID_BRIM_TYPES,
    VALID_INFILL_PATTERNS,
    VALID_SUPPORT_TYPES,
    ModelTooBigError,
    SlicingError,
    materialize_profiles_for_binary,
)


USER_PROFILE_CATEGORIES: tuple[str, ...] = ("filament", "process", "machine")


def _ensure_user_profile_dirs() -> None:
    """Create the typed user-profile subfolders if they don't exist.

    Also creates the per-category `base/` subfolder, mirroring OrcaSlicer's
    GUI layout for inherits-less ("detached") user presets — see
    `Preset.cpp::path_from_name` and `is_base_preset` in OrcaSlicer source.
    """
    for category in USER_PROFILE_CATEGORIES:
        os.makedirs(os.path.join(USER_PROFILES_DIR, category, "base"), exist_ok=True)


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("orcaslicer-cli %s (commit %s)", VERSION, GIT_COMMIT)
    _ensure_user_profile_dirs()
    app.state.token_cache = TokenCache(
        cache_dir=cfg.CACHE_DIR,
        max_bytes=cfg.CACHE_MAX_BYTES,
        max_files=cfg.CACHE_MAX_FILES,
    )
    app.state.inspect_cache = InspectCache()
    load_all_profiles()
    yield


app = FastAPI(
    title="OrcaSlicer CLI API",
    version=VERSION,
    description="REST API for headless 3D print slicing powered by OrcaSlicer. "
    "Loads Bambu Lab printer, process, and filament profiles and exposes "
    "endpoints to list them and slice `.3mf` files.",
    lifespan=lifespan,
)


@app.exception_handler(ProfileNotFoundError)
async def profile_not_found_handler(request, exc: ProfileNotFoundError):
    return JSONResponse(status_code=400, content={"error": str(exc)})


@app.exception_handler(ModelTooBigError)
async def model_too_big_handler(request, exc: ModelTooBigError):
    return JSONResponse(status_code=400, content={"error": str(exc)})


@app.exception_handler(ValueError)
async def value_error_handler(request, exc: ValueError):
    return JSONResponse(status_code=400, content={"error": str(exc)})


@app.exception_handler(SlicingError)
async def slicing_error_handler(request, exc: SlicingError):
    # Critical warnings (e.g. "model has floating regions, enable support")
    # represent model/settings problems the caller can act on — not server
    # faults — so they return 422 with the parsed messages surfaced.
    status_code = 422 if exc.critical_warnings else 500
    return JSONResponse(
        status_code=status_code,
        content={
            "error": str(exc),
            "orca_output": exc.orca_output,
            "critical_warnings": exc.critical_warnings,
        },
    )


def _typed_user_profile_path(category: str, setting_id: str) -> str:
    """Canonical write path for a derivative (has `inherits`) user profile."""
    return os.path.join(USER_PROFILES_DIR, category, f"{setting_id}.json")


def _base_user_profile_path(category: str, setting_id: str) -> str:
    """Canonical write path for a detached (no `inherits`) user profile.

    Mirrors OrcaSlicer's GUI which writes inherits-less presets under a
    `base/` subdir of the user preset directory.
    """
    return os.path.join(USER_PROFILES_DIR, category, "base", f"{setting_id}.json")


def _user_profile_path_for(category: str, setting_id: str, has_inherits: bool) -> str:
    """Pick the typed-vs-base write path based on whether the payload inherits."""
    return (
        _typed_user_profile_path(category, setting_id)
        if has_inherits
        else _base_user_profile_path(category, setting_id)
    )


def _legacy_user_profile_path(setting_id: str) -> str:
    """Pre-typed-layout write path used by older imports at the data root."""
    return os.path.join(USER_PROFILES_DIR, f"{setting_id}.json")


def _find_existing_user_profile(category: str, setting_id: str) -> str | None:
    """Return the path of an existing user profile file, or None.

    Search order: typed (with-inherits) → typed base/ → legacy flat root.
    Earlier-found wins, but for collision purposes any of the three blocks
    a fresh import for the same setting_id.
    """
    for path in (
        _typed_user_profile_path(category, setting_id),
        _base_user_profile_path(category, setting_id),
        _legacy_user_profile_path(setting_id),
    ):
        if os.path.isfile(path):
            return path
    return None


def _reject_unsafe_setting_id(setting_id: str) -> JSONResponse | None:
    """Reject setting_ids that would escape USER_PROFILES_DIR or be otherwise unsafe as a filename.

    Any path separator, parent-directory reference, or empty/whitespace value is blocked.
    """
    if (
        not isinstance(setting_id, str)
        or not setting_id.strip()
        or "/" in setting_id
        or "\\" in setting_id
        or ".." in setting_id
        or "\x00" in setting_id
    ):
        return JSONResponse(
            status_code=400,
            content={"error": f"Unsafe or invalid profile setting_id: {setting_id!r}"},
        )
    return None


def _read_filament_import_body(data: Any) -> tuple[dict | None, JSONResponse | None]:
    if not isinstance(data, dict):
        return None, JSONResponse(status_code=400, content={"error": "Body must be a JSON object."})

    name = data.get("name")
    if not name or not isinstance(name, str):
        return None, JSONResponse(status_code=400, content={"error": "Missing or invalid 'name' field."})

    try:
        return materialize_filament_import(data), None
    except (ProfileNotFoundError, ValueError) as exc:
        return None, JSONResponse(
            status_code=400,
            content={"error": str(exc)},
        )


@app.get("/health", response_model=HealthResponse, tags=["Health"])
async def health():
    """Check that the API is running and return its version."""
    return HealthResponse(status="ok", version=VERSION)


@app.get("/profiles/machines", response_model=list[MachineProfile], tags=["Profiles"])
async def list_machines():
    """List all available printer/machine profiles."""
    return get_machine_profiles()


@app.get("/profiles/processes", response_model=list[ProcessProfile], tags=["Profiles"])
async def list_processes(
    machine: str | None = Query(None, description="Filter by machine setting_id (e.g. GM014)."),
):
    """List process profiles, optionally filtered by a machine setting_id."""
    return get_process_profiles(machine_id=machine)


@app.get("/profiles/filaments", response_model=list[FilamentProfile], tags=["Profiles"])
async def list_filaments(
    machine: str | None = Query(None, description="Filter by machine setting_id (e.g. GM014)."),
    ams_assignable: bool = Query(
        False,
        description=(
            "If true, only include filament profiles that can be assigned to AMS "
            "(instantiable profile with non-empty setting_id and resolved filament_id)."
        ),
    ),
):
    """List filament profiles, optionally filtered by a machine setting_id."""
    return get_filament_profiles(machine_id=machine, ams_assignable_only=ams_assignable)


@app.get("/profiles/plate-types", response_model=list[PlateTypeOption], tags=["Profiles"])
async def list_plate_types():
    """List supported bed surface types for slicing."""
    return [
        {"value": value, "label": label}
        for value, label in PLATE_TYPE_API_TO_ORCA.items()
    ]


@app.post(
    "/profiles/filaments/resolve-import",
    response_model=FilamentProfileImportPreview,
    tags=["Profiles"],
)
async def resolve_filament_import(request: Request):
    """Preview the materialized + resolved view of a filament import payload.

    Returns the fully merged form in `resolved_profile` for inspection.
    The saved form on POST is the raw payload — clients should POST
    their original upload to `/profiles/filaments`, NOT this preview's
    `resolved_profile`.
    """
    try:
        raw_data = await request.json()
    except Exception:
        return JSONResponse(status_code=400, content={"error": "Invalid JSON body."})

    data, error_response = _read_filament_import_body(raw_data)
    if error_response is not None or data is None:
        return error_response

    try:
        merged = _resolve_chain_for_payload(data, category="filament")
    except ProfileNotFoundError as exc:
        return JSONResponse(status_code=400, content={"error": str(exc)})

    filament_type = merged.get("filament_type", "")
    if isinstance(filament_type, list):
        filament_type = filament_type[0] if filament_type else ""

    return FilamentProfileImportPreview(
        setting_id=data["setting_id"],
        filament_id=str(merged.get("filament_id", "")),
        name=str(merged.get("name", "")),
        filament_type=str(filament_type or ""),
        resolved_profile=merged,
    )


def _read_process_import_body(data: Any) -> tuple[dict | None, JSONResponse | None]:
    if not isinstance(data, dict):
        return None, JSONResponse(
            status_code=400,
            content={"error": "Body must be a JSON object."},
        )

    name = data.get("name")
    if not name or not isinstance(name, str):
        return None, JSONResponse(
            status_code=400,
            content={"error": "Missing or invalid 'name' field."},
        )

    try:
        return materialize_process_import(data), None
    except (ProfileNotFoundError, ValueError) as exc:
        return None, JSONResponse(
            status_code=400,
            content={"error": str(exc)},
        )


@app.post(
    "/profiles/processes/resolve-import",
    response_model=ProcessProfileImportPreview,
    tags=["Profiles"],
)
async def resolve_process_import(request: Request):
    """Preview the materialized + resolved view of a process import payload."""
    try:
        raw_data = await request.json()
    except Exception:
        return JSONResponse(status_code=400, content={"error": "Invalid JSON body."})

    data, error_response = _read_process_import_body(raw_data)
    if error_response is not None or data is None:
        return error_response

    try:
        merged = _resolve_chain_for_payload(data, category="process")
    except ProfileNotFoundError as exc:
        return JSONResponse(status_code=400, content={"error": str(exc)})

    inherits_resolved = ""
    if isinstance(raw_data, dict):
        raw_inherits = raw_data.get("inherits")
        if isinstance(raw_inherits, str):
            inherits_resolved = raw_inherits.strip()

    return ProcessProfileImportPreview(
        setting_id=data["setting_id"],
        name=str(merged.get("name", "")),
        inherits_resolved=inherits_resolved,
        resolved_profile=merged,
    )


@app.post(
    "/profiles/processes",
    response_model=ProcessProfileImportResponse,
    tags=["Profiles"],
)
async def import_process_profile(request: Request, replace: bool = False):
    """Import a custom process profile from JSON.

    Returns 201 on create, 200 on replace, 409 when the target file exists
    and `replace=true` was not provided.
    """
    try:
        data = await request.json()
    except Exception:
        return JSONResponse(status_code=400, content={"error": "Invalid JSON body."})

    data, error_response = _read_process_import_body(data)
    if error_response is not None or data is None:
        return error_response

    setting_id = data["setting_id"]
    unsafe = _reject_unsafe_setting_id(setting_id)
    if unsafe is not None:
        return unsafe

    has_inherits = bool(str(data.get("inherits") or "").strip())
    file_path = _user_profile_path_for("process", setting_id, has_inherits)
    existing_path = _find_existing_user_profile("process", setting_id)
    exists = existing_path is not None
    if exists and not replace:
        return JSONResponse(
            status_code=409,
            content={
                "error": (
                    f"Profile '{setting_id}' already exists. "
                    f"Pass ?replace=true to overwrite."
                )
            },
        )

    os.makedirs(os.path.dirname(file_path), exist_ok=True)
    with open(file_path, "w") as f:
        json.dump(data, f, indent=2)

    # Migrate-on-touch: if the existing file lived at the legacy flat
    # root, drop it now that we've written the typed copy.
    if exists and existing_path != file_path:
        try:
            os.remove(existing_path)
        except OSError:
            logger.warning("Failed to remove legacy file %s", existing_path)

    load_all_profiles()

    # Derive response fields from the resolved chain for consistency
    # with the filament endpoint and to surface the canonical name even
    # if a thin import only sets `inherits`. Falls back to the raw
    # saved payload if resolution fails for any reason.
    try:
        resolved = get_profile("process", setting_id)
    except ProfileNotFoundError:
        resolved = data

    name = str(resolved.get("name", ""))
    return JSONResponse(
        status_code=200 if exists else 201,
        content=ProcessProfileImportResponse(
            setting_id=setting_id,
            name=name,
            message=f"Profile '{name}' imported successfully.",
        ).model_dump(),
    )


@app.delete(
    "/profiles/processes/{setting_id}",
    response_model=ProcessProfileDeleteResponse,
    tags=["Profiles"],
)
async def delete_process_profile(setting_id: str):
    """Delete a user-imported custom process profile."""
    unsafe = _reject_unsafe_setting_id(setting_id)
    if unsafe is not None:
        return unsafe
    file_path = _find_existing_user_profile("process", setting_id)
    if file_path is None:
        return JSONResponse(
            status_code=404,
            content={"error": f"User process profile '{setting_id}' not found."},
        )

    os.remove(file_path)
    load_all_profiles()

    return ProcessProfileDeleteResponse(
        setting_id=setting_id,
        message=f"Profile '{setting_id}' deleted successfully.",
    )


@app.post(
    "/profiles/filaments",
    response_model=FilamentProfileImportResponse,
    tags=["Profiles"],
)
async def import_filament_profile(request: Request, replace: bool = False):
    """Import a custom filament profile from JSON.

    Returns 201 on create, 200 on replace, 409 when the target file exists
    and `replace=true` was not provided.
    """
    try:
        data = await request.json()
    except Exception:
        return JSONResponse(status_code=400, content={"error": "Invalid JSON body."})

    data, error_response = _read_filament_import_body(data)
    if error_response is not None or data is None:
        return error_response

    setting_id = data["setting_id"]
    unsafe = _reject_unsafe_setting_id(setting_id)
    if unsafe is not None:
        return unsafe

    has_inherits = bool(str(data.get("inherits") or "").strip())
    file_path = _user_profile_path_for("filament", setting_id, has_inherits)
    existing_path = _find_existing_user_profile("filament", setting_id)
    exists = existing_path is not None
    if exists and not replace:
        return JSONResponse(
            status_code=409,
            content={
                "error": (
                    f"Profile '{setting_id}' already exists. "
                    f"Pass ?replace=true to overwrite."
                )
            },
        )

    os.makedirs(os.path.dirname(file_path), exist_ok=True)
    with open(file_path, "w") as f:
        json.dump(data, f, indent=2)

    if exists and existing_path != file_path:
        try:
            os.remove(existing_path)
        except OSError:
            logger.warning("Failed to remove legacy file %s", existing_path)

    load_all_profiles()

    # Derive response fields from the resolved chain so thin imports
    # report parent-inherited values (e.g. `filament_type` from parent).
    # Falls back to the raw saved payload if resolution fails for any
    # reason (the file is already on disk and indexed).
    try:
        resolved = get_profile("filament", setting_id)
    except ProfileNotFoundError:
        resolved = data

    filament_type = resolved.get("filament_type", "")
    if isinstance(filament_type, list):
        filament_type = filament_type[0] if filament_type else ""

    return JSONResponse(
        status_code=200 if exists else 201,
        content=FilamentProfileImportResponse(
            setting_id=setting_id,
            filament_id=str(resolved.get("filament_id", "")),
            name=str(resolved.get("name", "")),
            filament_type=filament_type,
            message=f"Profile '{str(resolved.get('name', ''))}' imported successfully.",
        ).model_dump(),
    )


@app.delete(
    "/profiles/filaments/{setting_id}",
    response_model=FilamentProfileDeleteResponse,
    tags=["Profiles"],
)
async def delete_filament_profile(setting_id: str):
    """Delete a user-imported custom filament profile."""
    unsafe = _reject_unsafe_setting_id(setting_id)
    if unsafe is not None:
        return unsafe
    file_path = _find_existing_user_profile("filament", setting_id)
    if file_path is None:
        return JSONResponse(
            status_code=404,
            content={"error": f"User filament profile '{setting_id}' not found."},
        )

    os.remove(file_path)
    load_all_profiles()

    return FilamentProfileDeleteResponse(
        setting_id=setting_id,
        message=f"Profile '{setting_id}' deleted successfully.",
    )


@app.post("/profiles/reload", response_model=ReloadResponse, tags=["Profiles"])
async def reload_profiles():
    """Hot-reload all profiles (vendor + user) from disk."""
    summary = load_all_profiles()
    return ReloadResponse(**summary)


@app.get("/profiles/machines/{setting_id}", tags=["Profiles"])
async def get_machine_detail(setting_id: str):
    """Return a fully-resolved machine profile with inheritance chain."""
    try:
        return get_profile_detail("machine", setting_id)
    except ProfileNotFoundError as exc:
        return JSONResponse(status_code=404, content={"error": str(exc)})


@app.get("/profiles/processes/{setting_id}", tags=["Profiles"])
async def get_process_detail(setting_id: str):
    """Return a fully-resolved process profile with inheritance chain."""
    try:
        return get_profile_detail("process", setting_id)
    except ProfileNotFoundError as exc:
        return JSONResponse(status_code=404, content={"error": str(exc)})


@app.get("/profiles/filaments/{setting_id}", tags=["Profiles"])
async def get_filament_detail(setting_id: str):
    """Return a fully-resolved filament profile with inheritance chain."""
    try:
        return get_profile_detail("filament", setting_id)
    except ProfileNotFoundError as exc:
        return JSONResponse(status_code=404, content={"error": str(exc)})


@app.get(
    "/profiles/filaments/{setting_id}/export",
    tags=["Profiles"],
    responses={
        200: {
            "content": {
                "application/json": {},
                "application/zip": {},
            },
            "description": "User filament export — JSON for thin, zip for flattened.",
        },
        400: {"description": "Invalid shape parameter."},
        404: {"description": "User filament not found."},
        500: {"description": "Inheritance chain could not be resolved."},
    },
)
async def export_filament_profile(setting_id: str, shape: str = "flattened"):
    """Export a user filament for OrcaSlicer GUI import.

    `shape=flattened` (default) returns a zip with one JSON entry per
    compatible printer, each shaped for the GUI's
    `user/<profile>/filament/base/` directory and AMS-assignable on
    import.

    `shape=thin` returns the saved file as-is (with `inherits`
    preserved). The recipient OrcaSlicer install must already have the
    parent profile, and the imported result is not AMS-assignable.
    """
    if shape not in ("thin", "flattened"):
        return JSONResponse(
            status_code=400,
            content={"error": f"Invalid shape '{shape}'; expected 'thin' or 'flattened'."},
        )

    try:
        entries = export_user_filament(setting_id, shape=shape)
    except UnresolvedChainError as e:
        logger.warning("Export of '%s' failed: %s", setting_id, e)
        return JSONResponse(status_code=500, content={"error": str(e)})
    except ProfileNotFoundError as e:
        return JSONResponse(status_code=404, content={"error": str(e)})
    except ValueError as e:
        return JSONResponse(status_code=400, content={"error": str(e)})

    if shape == "thin":
        filename, data = entries[0]
        return Response(
            content=json.dumps(data, indent=2),
            media_type="application/json",
            headers={
                "Content-Disposition": f'attachment; filename="{filename}"',
            },
        )

    # flattened — package as zip
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for filename, data in entries:
            zf.writestr(filename, json.dumps(data, indent=2))
    alias = _filament_alias(str(entries[0][1].get("name", setting_id)))
    zip_filename = _safe_filename(
        alias or setting_id, fallback=setting_id,
    ).replace(".json", ".zip")
    return Response(
        content=buf.getvalue(),
        media_type="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="{zip_filename}"',
        },
    )


@app.post(
    "/profiles/filaments/export-batch",
    tags=["Profiles"],
    responses={
        200: {
            "content": {"application/zip": {}},
            "description": (
                "Zip of exported user filaments. The `X-Export-Skipped` "
                "header (when present) is a JSON-encoded object mapping "
                "skipped setting_ids to reasons "
                "(`not_found`, `unresolved_chain`, `no_compatible_printers`)."
            ),
        },
        400: {"description": "Invalid request body or shape."},
    },
)
async def export_filaments_batch(request: Request):
    """Batch-export a list of user filaments as a zip.

    Request body: `{"setting_ids": [...], "shape": "thin" | "flattened"}`.
    `shape` defaults to `"flattened"`.
    """
    try:
        body = await request.json()
    except Exception:
        return JSONResponse(status_code=400, content={"error": "Invalid JSON body."})

    if not isinstance(body, dict):
        return JSONResponse(status_code=400, content={"error": "Body must be a JSON object."})

    setting_ids = body.get("setting_ids")
    shape = body.get("shape", "flattened")

    if not isinstance(setting_ids, list) or not setting_ids:
        return JSONResponse(
            status_code=400,
            content={"error": "Missing or empty 'setting_ids' (must be a non-empty list)."},
        )

    if shape not in ("thin", "flattened"):
        return JSONResponse(
            status_code=400,
            content={"error": f"Invalid shape '{shape}'; expected 'thin' or 'flattened'."},
        )

    skipped: dict[str, str] = {}
    success_entries: list[tuple[str, dict]] = []

    for sid in setting_ids:
        if not isinstance(sid, str):
            skipped[str(sid)] = "not_found"
            continue
        try:
            entries = export_user_filament(sid, shape=shape)
        except UnresolvedChainError:
            skipped[sid] = "unresolved_chain"
            continue
        except ProfileNotFoundError:
            skipped[sid] = "not_found"
            continue
        except ValueError:
            skipped[sid] = "no_compatible_printers"
            continue
        success_entries.extend(entries)

    # Deduplicate filenames within the zip with `-2`, `-3`, ... suffixes.
    seen_names: dict[str, int] = {}
    deduped: list[tuple[str, dict]] = []
    for filename, data in success_entries:
        if filename not in seen_names:
            seen_names[filename] = 1
            deduped.append((filename, data))
            continue
        seen_names[filename] += 1
        stem, dot, ext = filename.rpartition(".")
        new_name = f"{stem}-{seen_names[filename]}.{ext}" if dot else f"{filename}-{seen_names[filename]}"
        deduped.append((new_name, data))

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for filename, data in deduped:
            zf.writestr(filename, json.dumps(data, indent=2))

    headers = {
        "Content-Disposition": (
            f'attachment; filename="user-filaments-'
            f'{datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")}.zip"'
        ),
    }
    if skipped:
        headers["X-Export-Skipped"] = json.dumps(skipped)

    return Response(
        content=buf.getvalue(),
        media_type="application/zip",
        headers=headers,
    )


@app.post("/3mf", tags=["3MF"])
async def upload_3mf(request: Request, file: UploadFile = File(...)):
    """Upload a .3mf file and receive a stable token for use with /slice/v2."""
    payload = await file.read()
    cache: TokenCache = request.app.state.token_cache
    token, sha, size, evicted = cache.put(payload)
    return {"token": token, "sha256": sha, "size": size, "evicts": evicted}


@app.delete("/3mf/cache", tags=["3MF"])
async def clear_cache(request: Request):
    """Evict all entries from the 3MF token cache."""
    cache: TokenCache = request.app.state.token_cache
    count, freed = cache.clear()
    return {"evicted": count, "freed_bytes": freed}


@app.get("/3mf/cache/stats", tags=["3MF"])
async def cache_stats(request: Request):
    """Return current 3MF token cache statistics."""
    cache: TokenCache = request.app.state.token_cache
    return cache.stats()


@app.get("/3mf/{token}", tags=["3MF"])
async def download_3mf(request: Request, token: str):
    """Download a previously uploaded .3mf file by token."""
    cache: TokenCache = request.app.state.token_cache
    try:
        path = cache.path(token)
    except KeyError:
        return JSONResponse(
            status_code=404,
            content={"code": "token_unknown", "token": token},
        )
    return FileResponse(
        path,
        media_type="application/vnd.ms-package.3dmanufacturing-3dmodel+xml",
    )


@app.get("/3mf/{token}/inspect", tags=["3MF"])
async def inspect_3mf(token: str, request: Request) -> JSONResponse:
    """Return a cheap structured summary of a cached 3MF.

    Pure read — does not slice. For un-sliced 3MFs `used_filament_indices`
    on each plate is `None`; a later task wires `orca-headless use-set` to
    populate it.
    """
    cache: TokenCache = request.app.state.token_cache
    inspect_cache: InspectCache = request.app.state.inspect_cache
    try:
        path = cache.path(token)
        sha256 = cache.sha256_for(token)
    except KeyError:
        return JSONResponse(
            status_code=404,
            content={"code": "token_unknown", "token": token},
        )
    cached = inspect_cache.get(sha256)
    if cached is not None:
        return JSONResponse(content=cached)
    file_bytes = path.read_bytes()
    data = parse_inspect_data(file_bytes)
    thumbs = list_plate_thumbnails(file_bytes)
    data["thumbnail_urls"] = [
        {
            "plate": t["plate"],
            "kind": t["kind"],
            "url": f"/3mf/{token}/plates/{t['plate']}/thumbnail?kind={t['kind']}",
        }
        for t in thumbs
    ]

    # Populate use_set_per_plate. Sliced 3MFs already carry per-plate
    # used-slot data via `slice_info.config` (parse_inspect_data fills
    # `plates[i].used_filament_indices` from there). For un-sliced 3MFs
    # we shell out to `orca-headless use-set`.
    use_set_per_plate: dict[int, list[int]] = {}
    needs_binary = any(
        p["used_filament_indices"] is None for p in data["plates"]
    )
    if needs_binary and cfg.USE_HEADLESS_BINARY:
        binary = BinaryClient(binary_path=cfg.ORCA_HEADLESS_BINARY)
        try:
            us_response = await binary.use_set(input_3mf=str(path))
        except BinaryError as e:
            logger.warning(
                "use-set failed; returning inspect without per-plate slots: %s",
                e.message,
            )
        else:
            for p in us_response.get("plates", []):
                use_set_per_plate[p["id"]] = p["used_filament_indices"]
            # Backfill into data["plates"].
            for plate in data["plates"]:
                if plate["used_filament_indices"] is None and \
                        plate["id"] in use_set_per_plate:
                    plate["used_filament_indices"] = use_set_per_plate[plate["id"]]
    # Sliced-side data already in data["plates"][i] — also surface as
    # the dict-keyed shape for gateway convenience.
    for plate in data["plates"]:
        if plate["used_filament_indices"] is not None:
            use_set_per_plate.setdefault(plate["id"], plate["used_filament_indices"])
    data["use_set_per_plate"] = use_set_per_plate
    inspect_cache.put(sha256, data)
    return JSONResponse(content=data)


@app.get("/3mf/{token}/plates/{plate}/thumbnail", tags=["3MF"])
async def get_plate_thumbnail(
    token: str, plate: int, request: Request, kind: str = "main",
) -> Response:
    """Return the PNG thumbnail for a specific plate of a cached 3MF."""
    cache: TokenCache = request.app.state.token_cache
    try:
        path = cache.path(token)
    except KeyError:
        return JSONResponse(
            status_code=404,
            content={"code": "token_unknown", "token": token},
        )
    png = read_plate_thumbnail(path.read_bytes(), plate=plate, kind=kind)
    if png is None:
        return JSONResponse(
            status_code=404,
            content={
                "code": "thumbnail_not_found",
                "token": token, "plate": plate, "kind": kind,
            },
        )
    return Response(
        content=png,
        media_type="image/png",
        headers={"Cache-Control": "public, max-age=86400"},
    )


@app.delete("/3mf/{token}", status_code=fastapi_status.HTTP_204_NO_CONTENT, tags=["3MF"])
async def delete_token(request: Request, token: str):
    """Delete a previously uploaded .3mf file by token."""
    cache: TokenCache = request.app.state.token_cache
    try:
        sha256 = cache.sha256_for(token)
    except KeyError:
        sha256 = None
    deleted = cache.delete(token)
    if not deleted:
        return JSONResponse(status_code=404, content={"code": "token_unknown", "token": token})
    if sha256:
        request.app.state.inspect_cache.invalidate(sha256)
    return None


class SliceTokenRequest(BaseModel):
    input_token: str
    machine_id: str
    process_id: str
    filament_settings_ids: list[str]
    filament_map: list[int] | None = None
    plate_id: int = 1
    recenter: bool = True


@app.post("/slice/v2", tags=["Slice"])
async def slice_v2(request: Request, body: SliceTokenRequest):
    """Slice a previously-uploaded 3MF file using the headless binary.

    Accepts a JSON body referencing an uploaded token and profile setting_ids.
    Requires USE_HEADLESS_BINARY to be enabled.
    """
    cache: TokenCache = request.app.state.token_cache
    try:
        input_path = cache.path(body.input_token)
    except KeyError:
        return JSONResponse(
            status_code=404,
            content={"code": "token_unknown", "token": body.input_token},
        )

    if not cfg.USE_HEADLESS_BINARY:
        return JSONResponse(
            status_code=503,
            content={
                "code": "headless_disabled",
                "message": "USE_HEADLESS_BINARY is off; use legacy POST /slice",
            },
        )

    paths = await materialize_profiles_for_binary(
        machine_id=body.machine_id,
        process_id=body.process_id,
        filament_setting_ids=body.filament_settings_ids,
    )

    output_path = cache.cache_dir / f"sliced-{body.input_token[:8]}.3mf"
    binary = BinaryClient(binary_path=cfg.ORCA_HEADLESS_BINARY)
    try:
        result = await binary.slice(request={
            "input_3mf": str(input_path),
            "output_3mf": str(output_path),
            "machine_profile": paths["machine"],
            "process_profile": paths["process"],
            "filament_profiles": paths["filaments"],
            "plate_id": body.plate_id,
            "options": {"recenter": body.recenter},
            "filament_map": body.filament_map or [],
            "filament_settings_id": paths["filament_names"],
            "printer_model_id": paths.get("printer_model_id", ""),
        })
    except BinaryError as e:
        return JSONResponse(
            status_code=500,
            content={
                "code": e.code,
                "message": e.message,
                "details": e.details,
                "stderr_tail": e.stderr_tail,
            },
        )

    out_token, out_sha, out_size, _ = cache.put(output_path.read_bytes())

    return {
        "input_token": body.input_token,
        "output_token": out_token,
        "output_sha256": out_sha,
        "estimate": result["estimate"],
        "settings_transfer": result["settings_transfer"],
        "thumbnail_urls": [],
        "download_url": f"/3mf/{out_token}",
    }


@app.post("/slice-stream/v2", tags=["Slice"])
async def slice_stream_v2(request: Request, body: SliceTokenRequest):
    """Slice a previously-uploaded 3MF file and stream progress via SSE (headless binary).

    Events: `progress` (phase/percent) and `result` (estimate, tokens, download_url).
    Requires USE_HEADLESS_BINARY to be enabled.
    """
    cache: TokenCache = request.app.state.token_cache
    try:
        input_path = cache.path(body.input_token)
    except KeyError:
        return JSONResponse(404, content={"code": "token_unknown", "token": body.input_token})

    if not cfg.USE_HEADLESS_BINARY:
        return JSONResponse(503, content={
            "code": "headless_disabled",
            "message": "USE_HEADLESS_BINARY is off; use legacy POST /slice-stream",
        })

    paths = await materialize_profiles_for_binary(
        machine_id=body.machine_id,
        process_id=body.process_id,
        filament_setting_ids=body.filament_settings_ids,
    )
    output_path = cache.cache_dir / f"sliced-{body.input_token[:8]}.3mf"
    binary = BinaryClient(binary_path=cfg.ORCA_HEADLESS_BINARY)

    async def event_gen():
        async for ev in binary.slice_stream(request={
            "input_3mf": str(input_path),
            "output_3mf": str(output_path),
            "machine_profile": paths["machine"],
            "process_profile": paths["process"],
            "filament_profiles": paths["filaments"],
            "plate_id": body.plate_id,
            "options": {"recenter": body.recenter},
            "filament_map": body.filament_map or [],
            "filament_settings_id": paths["filament_names"],
            "printer_model_id": paths.get("printer_model_id", ""),
        }):
            if ev["type"] == "result":
                out_token, out_sha, out_size, _ = cache.put(output_path.read_bytes())
                ev["payload"] = {
                    "input_token": body.input_token,
                    "output_token": out_token,
                    "output_sha256": out_sha,
                    "estimate": ev["payload"]["estimate"],
                    "settings_transfer": ev["payload"]["settings_transfer"],
                    "download_url": f"/3mf/{out_token}",
                }
            yield f"event: {ev['type']}\ndata: {json.dumps(ev['payload'])}\n\n"

    return StreamingResponse(event_gen(), media_type="text/event-stream")


# Mount web UI — must be last so API routes take priority
app.mount("/web", StaticFiles(directory="app/web", html=True), name="web")
