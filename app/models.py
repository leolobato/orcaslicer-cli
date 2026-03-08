from pydantic import BaseModel, Field


class HealthResponse(BaseModel):
    """Health check response."""

    status: str = Field(examples=["ok"])
    version: str = Field(description="OrcaSlicer version and API revision.", examples=["2.3.1-1"])


class MachineProfile(BaseModel):
    """A printer/machine profile."""

    setting_id: str = Field(description="Profile identifier.", examples=["GM014"])
    name: str = Field(examples=["Bambu Lab P1S 0.4 nozzle"])
    nozzle_diameter: str = Field(examples=["0.4"])
    printer_model: str = Field(examples=["Bambu Lab P1S"])


class ProcessProfile(BaseModel):
    """A print process (quality/speed) profile."""

    setting_id: str = Field(description="Profile identifier.", examples=["GP004"])
    name: str = Field(examples=["0.20mm Standard @BBL P1S"])
    compatible_printers: list[str] = Field(description="Machine setting_ids this process is compatible with.")
    layer_height: str = Field(examples=["0.2"])


class FilamentProfile(BaseModel):
    """A filament material profile."""

    setting_id: str = Field(description="Profile identifier.", examples=["GFL99"])
    name: str = Field(examples=["Bambu PLA Basic @BBL P1S"])
    compatible_printers: list[str] = Field(description="Machine setting_ids this filament is compatible with.")
    filament_type: str = Field(examples=["PLA"])


class SliceError(BaseModel):
    """Error response from the slicing endpoint."""

    error: str = Field(description="Human-readable error message.")
    orca_output: str | None = Field(default=None, description="Raw output from OrcaSlicer, if available.")


class ReloadResponse(BaseModel):
    """Response from the profile reload endpoint."""

    machines: int = Field(description="Number of machine profiles loaded.")
    processes: int = Field(description="Number of process profiles loaded.")
    filaments: int = Field(description="Number of filament profiles loaded.")
    user: int = Field(description="Number of user-provided profiles loaded.")


class FilamentProfileImportResponse(BaseModel):
    """Response from importing a custom filament profile."""

    setting_id: str = Field(description="Profile identifier.")
    name: str = Field(description="Profile name.")
    filament_type: str = Field(description="Filament material type.", examples=["PLA"])
    message: str = Field(description="Status message.")
