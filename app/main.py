"""FastAPI app exposing OrcaSlicer as a REST API."""

import json
import logging
import os
from contextlib import asynccontextmanager
from typing import Any

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO").upper())
logger = logging.getLogger(__name__)

from fastapi import FastAPI, File, Form, Query, Request, UploadFile
from fastapi.responses import JSONResponse, Response
from starlette.responses import StreamingResponse

from .config import USER_PROFILES_DIR, VERSION
from .models import (
    FilamentProfile,
    FilamentProfileImportPreview,
    FilamentProfileImportResponse,
    HealthResponse,
    MachineProfile,
    PlateTypeOption,
    ProcessProfile,
    ReloadResponse,
    SliceError,
)
from .profiles import (
    ProfileNotFoundError,
    get_filament_profiles,
    get_machine_profiles,
    get_process_profiles,
    get_profile,
    load_all_profiles,
    materialize_filament_import,
)
from .slicer import (
    PLATE_TYPE_API_TO_ORCA,
    SUPPORTED_PLATE_TYPES,
    ModelTooBigError,
    SlicingError,
    slice_3mf,
    slice_3mf_streaming,
)


@asynccontextmanager
async def lifespan(app: FastAPI):
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


@app.exception_handler(SlicingError)
async def slicing_error_handler(request, exc: SlicingError):
    return JSONResponse(
        status_code=500,
        content={"error": str(exc), "orca_output": exc.orca_output},
    )


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
    """Resolve a filament import payload without saving it."""
    try:
        raw_data = await request.json()
    except Exception:
        return JSONResponse(status_code=400, content={"error": "Invalid JSON body."})

    data, error_response = _read_filament_import_body(raw_data)
    if error_response is not None or data is None:
        return error_response

    filament_type = data.get("filament_type", "")
    if isinstance(filament_type, list):
        filament_type = filament_type[0] if filament_type else ""

    return FilamentProfileImportPreview(
        setting_id=data["setting_id"],
        filament_id=str(data.get("filament_id", "")),
        name=str(data.get("name", "")),
        filament_type=str(filament_type or ""),
        resolved_payload=data,
    )


@app.post(
    "/profiles/filaments",
    response_model=FilamentProfileImportResponse,
    status_code=201,
    tags=["Profiles"],
)
async def import_filament_profile(request: Request):
    """Import a custom filament profile from JSON."""
    try:
        data = await request.json()
    except Exception:
        return JSONResponse(status_code=400, content={"error": "Invalid JSON body."})

    data, error_response = _read_filament_import_body(data)
    if error_response is not None or data is None:
        return error_response

    setting_id = data["setting_id"]

    os.makedirs(USER_PROFILES_DIR, exist_ok=True)
    file_path = os.path.join(USER_PROFILES_DIR, f"{setting_id}.json")
    with open(file_path, "w") as f:
        json.dump(data, f, indent=2)

    load_all_profiles()

    filament_type = data.get("filament_type", "")
    if isinstance(filament_type, list):
        filament_type = filament_type[0] if filament_type else ""

    return FilamentProfileImportResponse(
        setting_id=setting_id,
        filament_id=str(data.get("filament_id", "")),
        name=str(data.get("name", "")),
        filament_type=filament_type,
        message=f"Profile '{str(data.get('name', ''))}' imported successfully.",
    )


@app.post("/profiles/reload", response_model=ReloadResponse, tags=["Profiles"])
async def reload_profiles():
    """Hot-reload all profiles (vendor + user) from disk."""
    summary = load_all_profiles()
    return ReloadResponse(**summary)


@app.get("/profiles/filaments/{setting_id}", tags=["Profiles"])
async def get_filament_detail(setting_id: str):
    """Return a fully-resolved filament profile with all fields."""
    try:
        return get_profile("filament", setting_id)
    except ProfileNotFoundError as exc:
        return JSONResponse(status_code=404, content={"error": str(exc)})


@app.post(
    "/slice",
    tags=["Slicing"],
    summary="Slice a 3MF file",
    responses={
        200: {
            "description": "Sliced G-code inside a `.3mf` archive.",
            "content": {"application/octet-stream": {}},
        },
        400: {"description": "Invalid input (bad profiles or file).", "model": SliceError},
        500: {"description": "OrcaSlicer failed.", "model": SliceError},
    },
)
async def slice_file(
    file: UploadFile = File(description="A `.3mf` file to slice."),
    machine_profile: str = Form(description="Machine setting_id (e.g. GM014).", examples=["GM014"]),
    process_profile: str = Form(description="Process setting_id (e.g. GP004).", examples=["GP004"]),
    filament_profiles: str = Form(
        description='JSON array of filament setting_ids, e.g. `["GFL99"]`.',
        examples=['["GFL99"]'],
    ),
    plate_type: str | None = Form(
        default=None,
        description=(
            "Optional bed surface type. "
            "One of: cool_plate, engineering_plate, high_temp_plate, "
            "textured_pei_plate, textured_cool_plate, supertack_plate."
        ),
        examples=["textured_pei_plate"],
    ),
):
    """Slice a `.3mf` file using the specified machine, process, and filament profiles.

    Returns the sliced `.3mf` archive containing G-code.
    """
    # Parse filament_profiles JSON list
    try:
        filament_ids = json.loads(filament_profiles)
        if not isinstance(filament_ids, list):
            raise ValueError
    except (json.JSONDecodeError, ValueError):
        return JSONResponse(
            status_code=400,
            content={"error": "filament_profiles must be a JSON-encoded list of setting_id strings"},
        )

    file_bytes = await file.read()
    if not file_bytes:
        return JSONResponse(status_code=400, content={"error": "Empty file"})

    if plate_type is not None:
        plate_type = plate_type.strip().lower()
        if not plate_type:
            plate_type = None
    if plate_type and plate_type not in SUPPORTED_PLATE_TYPES:
        return JSONResponse(
            status_code=400,
            content={
                "error": (
                    f"plate_type must be one of: {', '.join(SUPPORTED_PLATE_TYPES)}"
                ),
            },
        )
    orca_plate_type = PLATE_TYPE_API_TO_ORCA[plate_type] if plate_type else None

    result, settings_transfer = await slice_3mf(
        file_bytes, machine_profile, process_profile, filament_ids, plate_type=orca_plate_type,
    )
    headers = {
        "Content-Disposition": "attachment; filename=sliced.3mf",
        "X-Settings-Transfer-Status": settings_transfer.status,
    }
    if settings_transfer.status == "applied" and settings_transfer.transferred:
        headers["X-Settings-Transferred"] = json.dumps(settings_transfer.transferred)
    return Response(
        content=result,
        media_type="application/octet-stream",
        headers=headers,
    )


@app.post(
    "/slice-stream",
    tags=["Slicing"],
    summary="Slice a 3MF file with streaming progress",
    responses={
        200: {
            "description": "SSE stream with progress events, result (base64), and done.",
            "content": {"text/event-stream": {}},
        },
        400: {"description": "Invalid input (bad profiles or file).", "model": SliceError},
    },
)
async def slice_file_stream(
    file: UploadFile = File(description="A `.3mf` file to slice."),
    machine_profile: str = Form(description="Machine setting_id (e.g. GM014).", examples=["GM014"]),
    process_profile: str = Form(description="Process setting_id (e.g. GP004).", examples=["GP004"]),
    filament_profiles: str = Form(
        description='JSON array of filament setting_ids, e.g. `["GFL99"]`.',
        examples=['["GFL99"]'],
    ),
    plate_type: str | None = Form(
        default=None,
        description=(
            "Optional bed surface type. "
            "One of: cool_plate, engineering_plate, high_temp_plate, "
            "textured_pei_plate, textured_cool_plate, supertack_plate."
        ),
        examples=["textured_pei_plate"],
    ),
):
    """Slice a `.3mf` file and stream progress via Server-Sent Events.

    Returns an SSE stream with event types: `status`, `progress`, `result`, `error`, `done`.
    The `result` event contains the sliced file as base64.
    """
    try:
        filament_ids = json.loads(filament_profiles)
        if not isinstance(filament_ids, list):
            raise ValueError
    except (json.JSONDecodeError, ValueError):
        return JSONResponse(
            status_code=400,
            content={"error": "filament_profiles must be a JSON-encoded list of setting_id strings"},
        )

    file_bytes = await file.read()
    if not file_bytes:
        return JSONResponse(status_code=400, content={"error": "Empty file"})

    if plate_type is not None:
        plate_type = plate_type.strip().lower()
        if not plate_type:
            plate_type = None
    if plate_type and plate_type not in SUPPORTED_PLATE_TYPES:
        return JSONResponse(
            status_code=400,
            content={
                "error": (
                    f"plate_type must be one of: {', '.join(SUPPORTED_PLATE_TYPES)}"
                ),
            },
        )
    orca_plate_type = PLATE_TYPE_API_TO_ORCA[plate_type] if plate_type else None

    generator = await slice_3mf_streaming(
        file_bytes, machine_profile, process_profile, filament_ids, plate_type=orca_plate_type,
    )
    return StreamingResponse(
        generator,
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
