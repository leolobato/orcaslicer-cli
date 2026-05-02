import os
from pathlib import Path

ORCA_VERSION = "2.3.2"
API_REVISION = "19"
VERSION = f"{ORCA_VERSION}-{API_REVISION}"

# Git commit baked in at image build time. The Dockerfile takes a
# ``GIT_COMMIT`` build arg and re-exposes it as an env var; ``deploy-docker.sh``
# (or ``docker compose build``) is expected to pass ``--build-arg
# GIT_COMMIT=$(git rev-parse HEAD)``. Falls back to ``"unknown"`` when running
# outside a baked image (e.g. local ``uvicorn`` against the source tree).
GIT_COMMIT = os.environ.get("GIT_COMMIT", "unknown")

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

CACHE_DIR = Path(os.environ.get("CACHE_DIR", "/data/cache"))
CACHE_MAX_BYTES = int(os.environ.get("CACHE_MAX_BYTES", str(10 * 1024 * 1024 * 1024)))  # 10 GB
CACHE_MAX_FILES = int(os.environ.get("CACHE_MAX_FILES", "200"))

USE_HEADLESS_BINARY = os.environ.get("USE_HEADLESS_BINARY", "0").lower() in ("1", "true", "yes")
ORCA_HEADLESS_BINARY = os.environ.get("ORCA_HEADLESS_BINARY", "/opt/orca-headless/bin/orca-headless")
