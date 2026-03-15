import os

ORCA_VERSION = "2.3.1"
API_REVISION = "1"
VERSION = f"{ORCA_VERSION}-{API_REVISION}"

ORCA_BINARY = os.environ.get("ORCA_BINARY", "/opt/orcaslicer/bin/orca-slicer")
PROFILES_DIR = os.environ.get("PROFILES_DIR", "/opt/orcaslicer/profiles")
USER_PROFILES_DIR = os.environ.get("USER_PROFILES_DIR", "/data")
