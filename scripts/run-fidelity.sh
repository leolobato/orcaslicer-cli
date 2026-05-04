#!/usr/bin/env bash
# Run the integration fidelity suite against a running orcaslicer-cli.
#
# Usage:
#   scripts/run-fidelity.sh                 # runs against http://10.0.1.9:8070
#   ORCASLICER_API=http://localhost:8070 scripts/run-fidelity.sh
#   scripts/run-fidelity.sh -k fixture_07    # filter to one test
#
# The fidelity tests slice each fixture through `/slice/v2` and compare
# the output's `slice_info.config` metadata + first toolpath XY against
# a stored GUI-sliced ground truth, plus targeted CONFIG_BLOCK key=value
# asserts that lock in specific fixes (filament_ids, enable_prime_tower,
# nozzle_temperature, curr_bed_type, print_sequence, etc.).
#
# Tolerances absorb scarf-joint / infill-seed jitter; an exceeded
# tolerance signals a real divergence from GUI behaviour.
#
# Skips automatically when the API is unreachable, so you can run this
# from CI without a container.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

export ORCASLICER_API="${ORCASLICER_API:-http://10.0.1.9:8070}"

if [ ! -d ".venv" ]; then
    echo "no .venv at $REPO_ROOT/.venv — create one first:" >&2
    echo "  python3 -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt pytest" >&2
    exit 1
fi

# shellcheck disable=SC1091
source .venv/bin/activate

echo "[fidelity] API target: $ORCASLICER_API"
if ! curl -sS -m 3 "$ORCASLICER_API/health" >/dev/null 2>&1; then
    echo "[fidelity] WARNING: $ORCASLICER_API/health unreachable; pytest will skip the suite" >&2
fi

exec python -m pytest tests/integration/test_slice_v2_fidelity.py -v "$@"
