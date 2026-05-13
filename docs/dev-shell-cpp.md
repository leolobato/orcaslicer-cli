# C++ dev shell — fast iteration loop

Compose builds run from scratch every time `cpp/` or `vendor/OrcaSlicer/`
changes (the `cpp-builder` stage's `COPY cpp cpp` invalidates cache and
the cmake step rebuilds the entire C++ tree, ~30 min). The dev-shell
workflow below makes incremental rebuilds **30–90 seconds** by
bind-mounting the source dirs into a long-lived container that keeps
ninja's build state between rebuilds, then `docker cp`-ing the binary
into the running runtime container.

## One-time setup

```bash
# Tag the cpp-builder stage as a reusable dev image. First call paid the
# full ~30 min; subsequent calls are cache hits (seconds).
docker build --platform linux/amd64 --target cpp-builder -t orca-cpp-builder:dev .

# Spin up a long-lived dev container with cpp/ AND vendor/ bind-mounted.
# Bind-mounting both lets ninja see edits to slice_mode.cpp AND vendored
# OrcaSlicer libslic3r files (Brim.cpp, Print.cpp, ...).
docker run -d --name orca-dev --platform linux/amd64 \
    -v "$(pwd)/cpp:/src/cpp" \
    -v "$(pwd)/vendor:/src/vendor" \
    orca-cpp-builder:dev sleep infinity
```

## Iteration cycle (~30s–8min depending on what changed)

```bash
# 1. Edit cpp/src/*.cpp or vendor/OrcaSlicer/src/libslic3r/*.cpp on the host.

# 2. Incremental ninja build. Use -j2 if you edit a header included by
#    many TUs (Print.hpp, Preset.hpp, etc.) — full -j4 OOMs OrbStack on
#    the wide fan-out recompile. -j2 is the safe default for header
#    edits; .cpp-only edits can use the default.
docker exec orca-dev sh -c "cd /src && cmake --build build --target orca-headless -- -j2"

# 3. Hot-swap the binary into the running runtime container. No
#    docker compose restart needed — the FastAPI server spawns the
#    binary per request and picks up the new file on the next call.
docker cp orca-dev:/src/build/orca-headless /tmp/orca-headless-dev
docker cp /tmp/orca-headless-dev orcaslicer-headless-orcaslicer-headless-1:/opt/orca-headless/bin/orca-headless

# 4. Run the fidelity suite (or a single fixture).
ORCASLICER_API=http://localhost:8070 ./scripts/run-fidelity.sh -k fixture_04
```

Typical timings on this machine (M-series + OrbStack):

| Edit                                          | Rebuild time  |
|-----------------------------------------------|---------------|
| `cpp/src/slice_mode.cpp` only                 | ~45 s         |
| `vendor/OrcaSlicer/src/libslic3r/Brim.cpp`    | ~50 s         |
| `vendor/OrcaSlicer/src/libslic3r/Print.cpp`   | ~80 s         |
| `vendor/OrcaSlicer/src/libslic3r/Print.hpp` * | ~8 min (-j2)  |

\* widely-included header → most of libslic3r recompiles.

## Adding diagnostic probes

When you need to inspect intermediate slicing state (config values, polygon
bboxes, geometry), add `std::cerr <<` probes directly inside the relevant
vendored file (`Print.cpp`, `Brim.cpp`, etc.) or inside `cpp/src/slice_mode.cpp`,
gate them on a one-shot env var (`std::getenv("MY_DEBUG")`), rebuild via the
loop above, recreate the runtime container with the env var set, hot-swap the
binary, run the failing fixture, read the output via `docker logs`, the API's
`stderr_tail` field on slice errors, or `docker exec ... cat /tmp/...`. Once
the bug is found, strip the probes — the production binary should not carry
debug instrumentation.

This was the technique used to localize the fixture 04 ClipperLib overflow
to `WipeTowerData::height` being uninitialized in upstream OrcaSlicer; see
`CLAUDE.md` for the resulting two-line patch.

## Cleanup

The dev shell is disposable; nothing it produces ships to prod (the
production image is built by `local/build-and-ship.sh` from a clean
docker compose build). Tear it down when you're done:

```bash
docker rm -f orca-dev
```

Or keep it running between sessions — the bind mounts pick up host edits
automatically, and the only state that matters is the build/ dir inside
the container.

## Reference: ClipperLib coord overflow triage

ClipperLib's "Coordinate outside allowed range" exception is generic and
tells you nothing about which polygon op or which input ran wild. The
threshold is `loRange = 0x3FFFFFFF` (~1.07e9 µm = 1073 mm). Coordinates
in libslic3r are scaled µm (multiply by 1e-6 for mm). Walk the funnel
with throwaway probes:

1. The progress JSON in `stderr_tail` tells you the last successful step
   (e.g. "Generating skirt & brim").
2. Add bbox-printing probes inside the suspect vendored function around
   each `offset_ex` / `union_` / `diff_ex` / polygon `translate`. The
   first probe whose bbox sits at INT64_MIN/MAX is your culprit.
3. Cross-check against a working multi-filament fixture (06 is the
   canonical one in this repo) to see which key differs.
4. Common fault modes: NaN→int cast (yields INT64_MIN), uninitialized
   member field (`Vec3d` / `BoundingBoxf` / `Vec2f` not zero-init in
   Release), missing assignment in a code-path branch.
