#!/usr/bin/env bash
# Smoke test for orca-headless. Run inside the runtime container or against
# an installed binary on the host.
set -euo pipefail

BIN="${ORCA_HEADLESS_BINARY:-/opt/orca-headless/bin/orca-headless}"

echo "== version check =="
"$BIN" --version

echo "== unknown command exits non-zero =="
if "$BIN" totally-not-a-command 2>/dev/null; then
    echo "FAIL: unknown command should exit non-zero"
    exit 1
fi

echo "== slice without stdin exits non-zero =="
if "$BIN" slice </dev/null 2>/dev/null; then
    echo "FAIL: slice with empty stdin should exit non-zero"
    exit 1
fi

echo "OK"
