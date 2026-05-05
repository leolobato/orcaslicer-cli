#!/usr/bin/env bash
# Redeploy a Portainer compose stack via API, re-pulling images.
#
# Equivalent to clicking Portainer's "Update the stack" → "Re-pull image
# and redeploy" button. Used by build-and-ship.sh to flip the running
# container to the just-shipped image without manual intervention.
#
# Usage:
#   scripts/portainer-redeploy.sh                    # defaults: stack 39 (bambu-gateway)
#   STACK_ID=39 ENDPOINT_ID=3 scripts/portainer-redeploy.sh
#   PORTAINER_URL=http://10.0.1.9:9002 scripts/portainer-redeploy.sh
#
# Token resolution (first match wins):
#   1. $PORTAINER_TOKEN env var
#   2. file at $PORTAINER_TOKEN_FILE
#   3. ~/.config/orcaslicer-cli/portainer-token
#
# Exits 0 on success, non-zero on failure. Prints a human summary to stderr
# and the new container ID(s) on stdout if available.

set -euo pipefail

PORTAINER_URL="${PORTAINER_URL:-http://10.0.1.9:9002}"
STACK_ID="${STACK_ID:-39}"
ENDPOINT_ID="${ENDPOINT_ID:-3}"
TOKEN_FILE="${PORTAINER_TOKEN_FILE:-$HOME/.config/orcaslicer-cli/portainer-token}"

# Resolve token.
if [ -n "${PORTAINER_TOKEN:-}" ]; then
    TOKEN="$PORTAINER_TOKEN"
elif [ -f "$TOKEN_FILE" ]; then
    TOKEN="$(tr -d '\r\n' < "$TOKEN_FILE")"
else
    echo "[portainer-redeploy] no token (env PORTAINER_TOKEN unset, $TOKEN_FILE missing) — skipping" >&2
    exit 0
fi

if [ -z "$TOKEN" ]; then
    echo "[portainer-redeploy] token resolved but empty — skipping" >&2
    exit 0
fi

# Fetch stack metadata + file content. Stack metadata gives us the existing
# Env array we have to round-trip in the PUT (empty array for our stack
# but kept for forward-compatibility if the stack ever grows env vars).
META_JSON="$(curl -sS -m 10 -H "X-API-Key: $TOKEN" \
    "$PORTAINER_URL/api/stacks/$STACK_ID")" || {
    echo "[portainer-redeploy] failed to fetch stack $STACK_ID" >&2
    exit 1
}

if echo "$META_JSON" | grep -q '"message"'; then
    echo "[portainer-redeploy] API error fetching stack: $META_JSON" >&2
    exit 1
fi

FILE_JSON="$(curl -sS -m 10 -H "X-API-Key: $TOKEN" \
    "$PORTAINER_URL/api/stacks/$STACK_ID/file")" || {
    echo "[portainer-redeploy] failed to fetch stack $STACK_ID file" >&2
    exit 1
}

# Build the PUT body in Python. Pass the API responses through env vars
# rather than shell interpolation — the StackFileContent contains literal
# `\n` characters that break Python triple-quoted-string parsing.
PAYLOAD="$(META_JSON="$META_JSON" FILE_JSON="$FILE_JSON" python3 -c "
import json, os
meta = json.loads(os.environ['META_JSON'])
file_d = json.loads(os.environ['FILE_JSON'])
print(json.dumps({
    'stackFileContent': file_d.get('StackFileContent', ''),
    'env': meta.get('Env') or [],
    'prune': False,
    # pullImage=false because our images live only on the host (build-and-ship.sh
    # uses `docker save | ssh | docker load`, no registry). With pullImage=true
    # Portainer tries to pull orcaslicer-cli:latest from Docker Hub and fails.
    # The container still recreates against the new local image because
    # `docker load` already replaced :latest before we got here.
    'pullImage': False,
}))
")"

echo "[portainer-redeploy] redeploying stack $STACK_ID with image re-pull..." >&2
START=$(date +%s)
RESP="$(curl -sS -m 120 -X PUT \
    -H "X-API-Key: $TOKEN" \
    -H "Content-Type: application/json" \
    -d "$PAYLOAD" \
    "$PORTAINER_URL/api/stacks/$STACK_ID?endpointId=$ENDPOINT_ID")" || {
    echo "[portainer-redeploy] PUT request failed" >&2
    exit 1
}
ELAPSED=$(( $(date +%s) - START ))

# Portainer returns the updated stack object on success, or {"message": "..."} on error.
if echo "$RESP" | grep -q '"message"'; then
    echo "[portainer-redeploy] redeploy failed (${ELAPSED}s): $RESP" >&2
    exit 1
fi

echo "[portainer-redeploy] stack $STACK_ID redeployed (${ELAPSED}s)" >&2
