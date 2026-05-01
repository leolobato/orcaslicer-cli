import os

ORCA_VERSION = "2.3.2"
API_REVISION = "18"
VERSION = f"{ORCA_VERSION}-{API_REVISION}"

ORCA_BINARY = os.environ.get("ORCA_BINARY", "/opt/orcaslicer/bin/orca-slicer")
PROFILES_DIR = os.environ.get("PROFILES_DIR", "/opt/orcaslicer/profiles")
USER_PROFILES_DIR = os.environ.get("USER_PROFILES_DIR", "/data")
# Where the OrcaSlicer binary resolves ``resources_dir()`` at runtime. Distinct
# from ``PROFILES_DIR`` because the Dockerfile keeps two separate copies of the
# extracted AppImage's resources tree (one for our Python loader, one for the
# binary). Anything OrcaSlicer reads at slice time — e.g. the
# ``BBL/machine_full/`` ``model_id`` lookup that stamps ``printer_model_id``
# onto ``slice_info.config`` — must live under this prefix, not ``PROFILES_DIR``.
ORCA_RESOURCES_DIR = os.environ.get("ORCA_RESOURCES_DIR", "/opt/resources")
