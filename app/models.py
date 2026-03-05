from pydantic import BaseModel


class HealthResponse(BaseModel):
    status: str
    version: str


class MachineProfile(BaseModel):
    setting_id: str
    name: str
    nozzle_diameter: str
    printer_model: str


class ProcessProfile(BaseModel):
    setting_id: str
    name: str
    compatible_printers: list[str]
    layer_height: str


class FilamentProfile(BaseModel):
    setting_id: str
    name: str
    compatible_printers: list[str]
    filament_type: str


class SliceError(BaseModel):
    error: str
    orca_output: str | None = None
