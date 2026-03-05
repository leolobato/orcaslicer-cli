"""FastAPI app exposing OrcaSlicer as a REST API."""

import json
import logging
import os
from contextlib import asynccontextmanager

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO").upper())
logger = logging.getLogger(__name__)

from fastapi import FastAPI, File, Form, Query, UploadFile
from fastapi.responses import JSONResponse, Response

from .config import VERSION
from .models import (
    FilamentProfile,
    HealthResponse,
    MachineProfile,
    ProcessProfile,
    SliceError,
)
from .profiles import ProfileNotFoundError, get_filament_profiles, get_machine_profiles, get_process_profiles, load_all_profiles
from .slicer import SlicingError, slice_3mf


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


@app.exception_handler(SlicingError)
async def slicing_error_handler(request, exc: SlicingError):
    return JSONResponse(
        status_code=500,
        content={"error": str(exc), "orca_output": exc.orca_output},
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
    machine: str | None = Query(None, description="Filter by machine setting_id."),
):
    """List process profiles, optionally filtered by a machine setting_id."""
    return get_process_profiles(machine_id=machine)


@app.get("/profiles/filaments", response_model=list[FilamentProfile], tags=["Profiles"])
async def list_filaments(
    machine: str | None = Query(None, description="Filter by machine setting_id."),
):
    """List filament profiles, optionally filtered by a machine setting_id."""
    return get_filament_profiles(machine_id=machine)


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
    machine_profile: str = Form(description="Machine setting_id.", examples=["GM014"]),
    process_profile: str = Form(description="Process setting_id.", examples=["GP004"]),
    filament_profiles: str = Form(
        description='JSON array of filament setting_ids, e.g. `["GFL99"]`.',
        examples=['["GFL99"]'],
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

    result = await slice_3mf(file_bytes, machine_profile, process_profile, filament_ids)
    return Response(
        content=result,
        media_type="application/octet-stream",
        headers={"Content-Disposition": "attachment; filename=sliced.3mf"},
    )
