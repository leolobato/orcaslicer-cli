#!/usr/bin/env bash
# Build orcaslicer-cli locally, tag, and ship the image to root@10.0.1.9.
#
# Why a wrapper instead of inlining `docker compose build | gzip | ssh`:
# - Streams BuildKit progress so you can see which step is running (the
#   default rawjson buffer hides everything until completion, which makes
#   a hang indistinguishable from slow progress).
# - Watchdog: aborts when no progress line appears for IDLE_TIMEOUT seconds.
#   Catches the OrbStack/swap-thrash hang we hit at -j4 — there the build
#   would sit for 30+ minutes burning IO without emitting any step output.
# - Hard wall-clock cap (HARD_TIMEOUT) as a backstop.
# - macOS desktop notification + audible bell on completion (success or
#   failure), so you don't have to keep checking the terminal.
#
# Usage:
#   scripts/build-and-ship.sh           # default settings
#   IDLE_TIMEOUT=600 scripts/build-and-ship.sh
#   HARD_TIMEOUT=2700 scripts/build-and-ship.sh
#   REMOTE=root@host scripts/build-and-ship.sh
#   SKIP_SHIP=1 scripts/build-and-ship.sh   # build + tag only, don't push

set -uo pipefail

REMOTE="${REMOTE:-root@10.0.1.9}"
IMAGE="${IMAGE:-orcaslicer-cli:latest}"
COMPOSE_IMAGE="${COMPOSE_IMAGE:-orcaslicer-cli-orcaslicer-cli:latest}"
IDLE_TIMEOUT="${IDLE_TIMEOUT:-300}"   # 5 min without any progress line → abort
HARD_TIMEOUT="${HARD_TIMEOUT:-2700}"  # 45 min total → abort
SKIP_SHIP="${SKIP_SHIP:-0}"

PROGRESS_LOG="$(mktemp -t orcaslicer-build.XXXXXX)"
trap 'rm -f "$PROGRESS_LOG"' EXIT

notify() {
    local title="$1" message="$2" sound="${3:-}"
    if command -v osascript >/dev/null 2>&1; then
        osascript -e "display notification \"$message\" with title \"$title\"${sound:+ sound name \"$sound\"}" 2>/dev/null || true
    fi
    printf '\a' >&2  # terminal bell
    printf '\n[%s] %s: %s\n' "$(date '+%H:%M:%S')" "$title" "$message" >&2
}

cleanup_build() {
    pkill -f "docker compose build orcaslicer-cli" 2>/dev/null || true
    pkill -f "docker-buildx bake" 2>/dev/null || true
    pkill -f "docker save orcaslicer-cli" 2>/dev/null || true
}

# 1. Build with streaming progress so we can watchdog idle output.
#    `--progress=plain` flushes one line per build step.
echo "[$(date '+%H:%M:%S')] starting build (idle=${IDLE_TIMEOUT}s, hard=${HARD_TIMEOUT}s)" >&2
docker compose build --progress=plain orcaslicer-cli 2>&1 | tee "$PROGRESS_LOG" &
BUILD_PID=$!

start_ts=$(date +%s)
last_size=0
last_change_ts=$start_ts

while kill -0 "$BUILD_PID" 2>/dev/null; do
    sleep 10
    now=$(date +%s)
    elapsed=$((now - start_ts))
    cur_size=$(wc -c < "$PROGRESS_LOG" 2>/dev/null || echo 0)

    if [ "$cur_size" -gt "$last_size" ]; then
        last_size=$cur_size
        last_change_ts=$now
    fi

    idle=$((now - last_change_ts))

    if [ "$idle" -ge "$IDLE_TIMEOUT" ]; then
        echo "[$(date '+%H:%M:%S')] no progress for ${idle}s — aborting (last log size=${cur_size})" >&2
        cleanup_build
        wait "$BUILD_PID" 2>/dev/null
        notify "orcaslicer-cli build" "Hung: no progress for ${idle}s. Aborted." "Basso"
        exit 124
    fi

    if [ "$elapsed" -ge "$HARD_TIMEOUT" ]; then
        echo "[$(date '+%H:%M:%S')] hard timeout ${HARD_TIMEOUT}s reached — aborting" >&2
        cleanup_build
        wait "$BUILD_PID" 2>/dev/null
        notify "orcaslicer-cli build" "Wall-clock timeout (${HARD_TIMEOUT}s). Aborted." "Basso"
        exit 124
    fi
done

wait "$BUILD_PID"
build_rc=$?
if [ "$build_rc" -ne 0 ]; then
    notify "orcaslicer-cli build" "Build failed (exit $build_rc)." "Basso"
    exit "$build_rc"
fi

# 2. Tag the compose-built image to the canonical name we ship.
docker tag "$COMPOSE_IMAGE" "$IMAGE" || {
    notify "orcaslicer-cli build" "docker tag failed." "Basso"
    exit 1
}
echo "[$(date '+%H:%M:%S')] tagged $COMPOSE_IMAGE → $IMAGE" >&2

if [ "$SKIP_SHIP" = "1" ]; then
    notify "orcaslicer-cli build" "Built locally (ship skipped)." "Glass"
    exit 0
fi

# 3. Save → gzip → ssh → load to remote, with the same idle/hard guards.
echo "[$(date '+%H:%M:%S')] shipping image to $REMOTE" >&2
ship_start=$(date +%s)
if ! docker save "$IMAGE" | gzip | ssh -o ServerAliveInterval=15 "$REMOTE" 'gunzip | docker load'; then
    notify "orcaslicer-cli build" "Ship to $REMOTE failed." "Basso"
    exit 1
fi
ship_elapsed=$(( $(date +%s) - ship_start ))
notify "orcaslicer-cli build" "Built + shipped to $REMOTE in $((($(date +%s) - start_ts) / 60))m (ship: ${ship_elapsed}s)." "Glass"

# 4. Trigger Portainer to recreate the bambu-gateway stack so the running
#    container flips to the just-shipped image. Without this, `docker load`
#    only stages the new image; the running container stays on the old one
#    until manually recreated. Skipped automatically when no Portainer
#    token is configured (prints a notice to stderr and exits 0).
#
# Pass `SKIP_REDEPLOY=1` to opt out (e.g. when shipping a debug build you
# don't want auto-flipped to live).
if [ "${SKIP_REDEPLOY:-0}" != "1" ]; then
    SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
    if [ -x "$SCRIPT_DIR/portainer-redeploy.sh" ]; then
        if "$SCRIPT_DIR/portainer-redeploy.sh"; then
            notify "orcaslicer-cli build" "Stack redeployed on $REMOTE — new container live." "Glass"
        else
            notify "orcaslicer-cli build" "Image shipped but Portainer redeploy failed — recreate manually." "Basso"
        fi
    fi
fi
