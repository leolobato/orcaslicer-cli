from typing import Literal

from pydantic import BaseModel, Field


class HealthResponse(BaseModel):
    """Health check response."""

    status: str = Field(examples=["ok"])
    version: str = Field(description="OrcaSlicer version and API revision.", examples=["2.3.2-1"])


class MachineProfile(BaseModel):
    """A printer/machine profile."""

    setting_id: str = Field(description="Profile identifier.", examples=["GM014"])
    name: str = Field(examples=["Bambu Lab P1S 0.4 nozzle"])
    vendor: str = Field(description="Vendor name.", examples=["BBL"])
    nozzle_diameter: str = Field(examples=["0.4"])
    printer_model: str = Field(examples=["Bambu Lab P1S"])


class ProcessProfile(BaseModel):
    """A print process (quality/speed) profile."""

    setting_id: str = Field(description="Profile identifier.", examples=["GP004"])
    name: str = Field(examples=["0.20mm Standard @BBL P1S"])
    vendor: str = Field(description="Vendor name.", examples=["BBL"])
    compatible_printers: list[str] = Field(description="Machine setting_ids this process is compatible with.")
    layer_height: str = Field(examples=["0.2"])


class FilamentProfile(BaseModel):
    """A filament material profile."""

    setting_id: str = Field(description="Profile identifier.", examples=["GFL99"])
    filament_id: str = Field(description="Filament identifier used for AMS assignment.", examples=["GFA00"])
    name: str = Field(examples=["Bambu PLA Basic @BBL P1S"])
    vendor: str = Field(description="Vendor name.", examples=["BBL"])
    compatible_printers: list[str] = Field(description="Machine setting_ids this filament is compatible with.")
    filament_type: str = Field(examples=["PLA"])
    ams_assignable: bool = Field(
        description=(
            "Whether this profile can be assigned to an AMS tray "
            "(instantiable profile with non-empty setting_id and resolved filament_id)."
        ),
        examples=[True],
    )


class PlateTypeOption(BaseModel):
    """A supported API plate type and its OrcaSlicer label."""

    value: str = Field(description="API value for requests.", examples=["textured_pei_plate"])
    label: str = Field(description="Human-readable OrcaSlicer plate name.", examples=["Textured PEI Plate"])


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
    filament_id: str = Field(description="Filament identifier used for AMS assignment.")
    name: str = Field(description="Profile name.")
    filament_type: str = Field(description="Filament material type.", examples=["PLA"])
    message: str = Field(description="Status message.")


class FilamentProfileDeleteResponse(BaseModel):
    """Response from deleting a custom filament profile."""

    setting_id: str = Field(description="Profile identifier that was deleted.")
    message: str = Field(description="Status message.")


class FilamentProfileImportPreview(BaseModel):
    """Resolved filament profile preview before saving."""

    setting_id: str = Field(description="Profile identifier.")
    filament_id: str = Field(description="Filament identifier used for AMS assignment.")
    name: str = Field(description="Profile name.")
    filament_type: str = Field(description="Resolved filament material type.", examples=["PLA"])
    resolved_profile: dict = Field(
        description=(
            "Fully merged filament profile (inheritance resolved) — informational. "
            "Send the original raw payload to POST /profiles/filaments, not this field."
        ),
    )


class ProcessProfileImportPreview(BaseModel):
    """Resolved process profile preview before saving."""

    setting_id: str = Field(description="Profile identifier.")
    name: str = Field(description="Profile name.")
    inherits_resolved: str = Field(
        default="",
        description="Name of the parent profile that the import resolved against.",
    )
    resolved_profile: dict = Field(
        description=(
            "Fully merged process profile (inheritance resolved) — informational. "
            "Send the original raw payload to POST /profiles/processes, not this field."
        ),
    )


class ProcessProfileImportResponse(BaseModel):
    """Response from importing a custom process profile."""

    setting_id: str = Field(description="Profile identifier.")
    name: str = Field(description="Profile name.")
    message: str = Field(description="Status message.")


class ProcessProfileDeleteResponse(BaseModel):
    """Response from deleting a custom process profile."""

    setting_id: str = Field(description="Profile identifier that was deleted.")
    message: str = Field(description="Status message.")


# ---------------------------------------------------------------------------
# /profiles/resolve-for-machine — GUI-equivalent compat fallback
# ---------------------------------------------------------------------------


class ResolveForMachineRequest(BaseModel):
    """Inputs for `POST /profiles/resolve-for-machine`.

    All current-state fields are optional: callers pass whichever ones the
    user has authored or selected. The slicer returns matching resolved
    blocks for each non-empty input.
    """

    machine_id: str = Field(
        description="Target machine setting_id (e.g. 'GM020').",
        examples=["GM020"],
    )
    process_name: str = Field(
        default="",
        description=(
            "Currently selected process profile (display name as it appears "
            "in the 3MF — e.g. '0.20mm Standard @BBL P2S')."
        ),
    )
    filament_names: list[str] = Field(
        default_factory=list,
        description=(
            "Currently selected filament names per slot, in declaration "
            "order. Display names (e.g. 'Bambu PLA Basic @BBL P2S')."
        ),
    )
    plate_type: str = Field(
        default="",
        description="Currently selected plate type (API value, e.g. 'supertack_plate').",
    )


FilamentMatchReason = Literal[
    "unchanged", "alias", "type", "default", "first_compat", "none",
]
ProcessMatchReason = Literal[
    "unchanged", "alias", "layer_height", "default", "first_compat", "none",
]
PlateTypeMatchReason = Literal["unchanged", "default", "none"]


class ResolvedFilament(BaseModel):
    slot: int = Field(description="0-indexed filament slot.")
    requested: str = Field(description="The name the caller asked about.")
    setting_id: str = Field(description="Resolved filament setting_id (empty when no compat candidate).")
    name: str = Field(description="Resolved filament display name (empty when no compat candidate).")
    alias: str = Field(description="Logical alias (e.g. 'Bambu PLA Basic').")
    match: FilamentMatchReason = Field(
        description=(
            "Why this profile was chosen: 'unchanged' (already compat), "
            "'alias' (same logical alias as requested), 'type' (same "
            "filament_type), 'default' (printer's default_filament_profile), "
            "'first_compat' (no scoring criterion matched), or 'none' (no "
            "compatible profile exists)."
        ),
    )


class ResolvedProcess(BaseModel):
    requested: str
    setting_id: str
    name: str
    alias: str
    match: ProcessMatchReason


class ResolvedPlateType(BaseModel):
    requested: str
    resolved: str = Field(description="API value for the resolved plate type.")
    match: PlateTypeMatchReason


class ResolveForMachineResponse(BaseModel):
    machine_id: str
    machine_name: str
    process: ResolvedProcess | None = None
    filaments: list[ResolvedFilament] = Field(default_factory=list)
    plate_type: ResolvedPlateType | None = None
