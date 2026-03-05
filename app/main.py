"""FastAPI app exposing OrcaSlicer as a REST API."""

import json
from contextlib import asynccontextmanager

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


app = FastAPI(title="OrcaSlicer CLI API", version=VERSION, lifespan=lifespan)


@app.exception_handler(ProfileNotFoundError)
async def profile_not_found_handler(request, exc: ProfileNotFoundError):
    return JSONResponse(status_code=400, content={"error": str(exc)})


@app.exception_handler(SlicingError)
async def slicing_error_handler(request, exc: SlicingError):
    return JSONResponse(
        status_code=500,
        content={"error": str(exc), "orca_output": exc.orca_output},
    )


@app.get("/health", response_model=HealthResponse)
async def health():
    return HealthResponse(status="ok", version=VERSION)


@app.get("/profiles/machines", response_model=list[MachineProfile])
async def list_machines():
    return get_machine_profiles()


@app.get("/profiles/processes", response_model=list[ProcessProfile])
async def list_processes(machine: str | None = Query(None)):
    return get_process_profiles(machine_id=machine)


@app.get("/profiles/filaments", response_model=list[FilamentProfile])
async def list_filaments(machine: str | None = Query(None)):
    return get_filament_profiles(machine_id=machine)


@app.post("/slice")
async def slice_file(
    file: UploadFile = File(...),
    machine_profile: str = Form(...),
    process_profile: str = Form(...),
    filament_profiles: str = Form(...),
):
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
