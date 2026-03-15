#!/usr/bin/env bash
# Quick smoke tests for the OrcaSlicer CLI API.
# Usage: ./test_api.sh [base_url]
#
# Expects the example 3MF files from ../bambu-poc/ to be present.
# Runs against http://localhost:8000 by default.

set -euo pipefail

BASE_URL="${1:-http://localhost:8000}"
EXAMPLES_DIR="../bambu-poc"
PASS=0
FAIL=0

green() { printf "\033[32m%s\033[0m\n" "$*"; }
red()   { printf "\033[31m%s\033[0m\n" "$*"; }

check() {
    local desc="$1" ok="$2"
    if [ "$ok" = "true" ]; then
        green "  PASS: $desc"
        PASS=$((PASS + 1))
    else
        red "  FAIL: $desc"
        FAIL=$((FAIL + 1))
    fi
}

echo "=== Health Check ==="
HEALTH=$(curl -sf "$BASE_URL/health")
check "returns status ok" "$(echo "$HEALTH" | python3 -c 'import sys,json; print("true" if json.load(sys.stdin)["status"]=="ok" else "false")')"
check "returns version" "$(echo "$HEALTH" | python3 -c 'import sys,json; v=json.load(sys.stdin)["version"]; print("true" if v and "-" in v else "false")')"

echo ""
echo "=== Machine Profiles ==="
MACHINES=$(curl -sf "$BASE_URL/profiles/machines")
COUNT=$(echo "$MACHINES" | python3 -c 'import sys,json; print(len(json.load(sys.stdin)))')
check "returns machines (got $COUNT)" "$([ "$COUNT" -gt 0 ] && echo true || echo false)"
check "A1 0.4 nozzle present (GM030)" "$(echo "$MACHINES" | python3 -c 'import sys,json; ms=json.load(sys.stdin); print("true" if any(m["setting_id"]=="GM030" for m in ms) else "false")')"
check "P1S 0.4 nozzle present (GM014)" "$(echo "$MACHINES" | python3 -c 'import sys,json; ms=json.load(sys.stdin); print("true" if any(m["setting_id"]=="GM014" for m in ms) else "false")')"

echo ""
echo "=== Process Profiles (filtered by P1S 0.4 = GM014) ==="
PROCS=$(curl -sf "$BASE_URL/profiles/processes?machine=GM014")
PCOUNT=$(echo "$PROCS" | python3 -c 'import sys,json; print(len(json.load(sys.stdin)))')
check "returns processes for P1S ($PCOUNT)" "$([ "$PCOUNT" -gt 0 ] && echo true || echo false)"
check "0.20mm Standard present (GP004)" "$(echo "$PROCS" | python3 -c 'import sys,json; ps=json.load(sys.stdin); print("true" if any(p["setting_id"]=="GP004" for p in ps) else "false")')"

echo ""
echo "=== Filament Profiles (filtered by A1 mini 0.4 = GM020) ==="
FILS=$(curl -sf "$BASE_URL/profiles/filaments?machine=GM020")
FCOUNT=$(echo "$FILS" | python3 -c 'import sys,json; print(len(json.load(sys.stdin)))')
check "returns filaments for A1M ($FCOUNT)" "$([ "$FCOUNT" -gt 0 ] && echo true || echo false)"
check "Generic PLA present (GFSL99_02)" "$(echo "$FILS" | python3 -c 'import sys,json; fs=json.load(sys.stdin); print("true" if any(f["setting_id"]=="GFSL99_02" for f in fs) else "false")')"
check "filaments expose filament_id" "$(echo "$FILS" | python3 -c 'import sys,json; fs=json.load(sys.stdin); print("true" if fs and all(bool(f.get(\"filament_id\")) for f in fs[:10]) else \"false\")')"

FILS_AMS=$(curl -sf "$BASE_URL/profiles/filaments?machine=GM020&ams_assignable=true")
FCOUNT_AMS=$(echo "$FILS_AMS" | python3 -c 'import sys,json; print(len(json.load(sys.stdin)))')
check "ams_assignable filter returns A1M filaments ($FCOUNT_AMS)" "$([ "$FCOUNT_AMS" -gt 0 ] && echo true || echo false)"
check "Bambu PLA Basic AMS profile present (GFSA00_02)" "$(echo "$FILS_AMS" | python3 -c 'import sys,json; fs=json.load(sys.stdin); print("true" if any(f["setting_id"]=="GFSA00_02" for f in fs) else "false")')"

echo ""
echo "=== Plate Types ==="
PLATES=$(curl -sf "$BASE_URL/profiles/plate-types")
PTCOUNT=$(echo "$PLATES" | python3 -c 'import sys,json; print(len(json.load(sys.stdin)))')
check "returns plate types ($PTCOUNT)" "$([ "$PTCOUNT" -gt 0 ] && echo true || echo false)"
check "textured_pei_plate present" "$(echo "$PLATES" | python3 -c 'import sys,json; ps=json.load(sys.stdin); print("true" if any(p.get("value")=="textured_pei_plate" for p in ps) else "false")')"

echo ""
echo "=== Error Handling ==="
ERR=$(curl -sf -o /dev/null -w "%{http_code}" "$BASE_URL/profiles/processes?machine=INVALID" || true)
check "invalid machine returns 400 (got $ERR)" "$([ "$ERR" = "400" ] && echo true || echo false)"

echo ""
echo "=== Slice: example3.3mf (A1 mini, 0.20mm Standard, Generic PLA) ==="
if [ -f "$EXAMPLES_DIR/example3.3mf" ]; then
    SLICE_OUT="test_output_example3.3mf"
    SLICE_HEADERS="test_headers_example3.txt"
    HTTP_CODE=$(curl -s -o "$SLICE_OUT" -D "$SLICE_HEADERS" -w "%{http_code}" \
        -F "file=@$EXAMPLES_DIR/example3.3mf" \
        -F "machine_profile=GM020" \
        -F "process_profile=GP000" \
        -F "plate_type=textured_pei_plate" \
        -F 'filament_profiles=["GFSL99_02"]' \
        "$BASE_URL/slice")
    check "slice returns 200 (got $HTTP_CODE)" "$([ "$HTTP_CODE" = "200" ] && echo true || echo false)"
    if [ "$HTTP_CODE" = "200" ]; then
        SIZE=$(wc -c < "$SLICE_OUT" | tr -d ' ')
        check "output is non-empty ($SIZE bytes)" "$([ "$SIZE" -gt 0 ] && echo true || echo false)"
        HAS_GCODE=$(python3 -c "
import zipfile, sys
try:
    with zipfile.ZipFile('$SLICE_OUT') as zf:
        has = any('plate_' in n and n.endswith('.gcode') for n in zf.namelist())
        print('true' if has else 'false')
except:
    print('false')
")
        check "output contains gcode" "$HAS_GCODE"
        TRANSFER_STATUS=$(grep -i 'x-settings-transfer-status' "$SLICE_HEADERS" | tr -d '\r' | awk '{print $2}')
        check "has X-Settings-Transfer-Status header ($TRANSFER_STATUS)" "$([ -n "$TRANSFER_STATUS" ] && echo true || echo false)"
    fi
    rm -f "$SLICE_OUT" "$SLICE_HEADERS"
else
    red "  SKIP: $EXAMPLES_DIR/example3.3mf not found"
fi

echo ""
echo "=== Slice: example.3mf (P1S, 0.20mm Standard, PLA Basic) ==="
if [ -f "$EXAMPLES_DIR/example.3mf" ]; then
    SLICE_OUT="test_output_example.3mf"
    HTTP_CODE=$(curl -s -o "$SLICE_OUT" -w "%{http_code}" \
        -F "file=@$EXAMPLES_DIR/example.3mf" \
        -F "machine_profile=GM014" \
        -F "process_profile=GP004" \
        -F 'filament_profiles=["GFSA00"]' \
        "$BASE_URL/slice")
    check "slice returns 200 (got $HTTP_CODE)" "$([ "$HTTP_CODE" = "200" ] && echo true || echo false)"
    if [ "$HTTP_CODE" = "200" ]; then
        SIZE=$(wc -c < "$SLICE_OUT" | tr -d ' ')
        check "output is non-empty ($SIZE bytes)" "$([ "$SIZE" -gt 0 ] && echo true || echo false)"
        HAS_GCODE=$(python3 -c "
import zipfile
try:
    with zipfile.ZipFile('$SLICE_OUT') as zf:
        has = any('plate_' in n and n.endswith('.gcode') for n in zf.namelist())
        print('true' if has else 'false')
except:
    print('false')
")
        check "output contains gcode" "$HAS_GCODE"
    fi
    rm -f "$SLICE_OUT"
else
    red "  SKIP: $EXAMPLES_DIR/example.3mf not found"
fi

echo ""
echo "=== Slice: example2.3mf (P1S, 0.28mm Extra Draft, 4 filaments) ==="
if [ -f "$EXAMPLES_DIR/example2.3mf" ]; then
    SLICE_OUT="test_output_example2.3mf"
    HTTP_CODE=$(curl -s -o "$SLICE_OUT" -w "%{http_code}" \
        -F "file=@$EXAMPLES_DIR/example2.3mf" \
        -F "machine_profile=GM014" \
        -F "process_profile=GP006" \
        -F 'filament_profiles=["GFSA00","GFSA00","GFSA00","GFSA00"]' \
        "$BASE_URL/slice")
    check "slice returns 200 (got $HTTP_CODE)" "$([ "$HTTP_CODE" = "200" ] && echo true || echo false)"
    if [ "$HTTP_CODE" = "200" ]; then
        SIZE=$(wc -c < "$SLICE_OUT" | tr -d ' ')
        check "output is non-empty ($SIZE bytes)" "$([ "$SIZE" -gt 0 ] && echo true || echo false)"
        HAS_GCODE=$(python3 -c "
import zipfile
try:
    with zipfile.ZipFile('$SLICE_OUT') as zf:
        has = any('plate_' in n and n.endswith('.gcode') for n in zf.namelist())
        print('true' if has else 'false')
except:
    print('false')
")
        check "output contains gcode" "$HAS_GCODE"
    fi
    rm -f "$SLICE_OUT"
else
    red "  SKIP: $EXAMPLES_DIR/example2.3mf not found"
fi

echo ""
echo "================================"
echo "Results: $PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ] && green "All tests passed!" || red "Some tests failed."
exit "$FAIL"
