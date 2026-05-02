# =============================================================================
# Stage 1: Extract pre-built OrcaSlicer from AppImage (legacy path; kept for
# back-compat during Phase 1 — switched off via USE_HEADLESS_BINARY).
# =============================================================================
FROM --platform=linux/amd64 ubuntu:24.04 AS appimage-extractor

RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates wget squashfs-tools && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /build

RUN wget --max-redirect=10 -q "https://github.com/OrcaSlicer/OrcaSlicer/releases/download/v2.3.2/OrcaSlicer_Linux_AppImage_Ubuntu2404_V2.3.2.AppImage" \
    -O orcaslicer.AppImage

# Extract squashfs from AppImage by computing ELF end offset
# (can't run --appimage-extract under QEMU emulation on arm64 host)
RUN ELF_END=$( \
      SHOFF=$(od -A n -t u8 -j 40 -N 8 orcaslicer.AppImage | tr -d ' ') && \
      SHENTSIZE=$(od -A n -t u2 -j 58 -N 2 orcaslicer.AppImage | tr -d ' ') && \
      SHNUM=$(od -A n -t u2 -j 60 -N 2 orcaslicer.AppImage | tr -d ' ') && \
      echo $((SHOFF + SHENTSIZE * SHNUM)) \
    ) && \
    tail -c +$((ELF_END + 1)) orcaslicer.AppImage > squashfs.img && \
    unsquashfs -d squashfs-root squashfs.img && \
    rm orcaslicer.AppImage squashfs.img

# =============================================================================
# Stage 2: Build OrcaSlicer's deps superbuild from the vendored source.
# Produces /deps/destdir/usr/local/{lib,include} containing CGAL,
# OpenCASCADE, OpenVDB, Boost, TBB, draco, OpenCV, JPEG, etc.
#
# Pinned to linux/amd64 to match the runtime stage (which must be amd64 to
# run the legacy AppImage binary). Once Phase 4 retires the AppImage, this
# pin can drop and builds become native-arch.
# =============================================================================
FROM --platform=linux/amd64 ubuntu:24.04 AS deps-builder

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    cmake \
    git \
    ninja-build \
    pkg-config \
    autoconf automake libtool \
    file \
    libssl-dev \
    libgl1-mesa-dev \
    libglu1-mesa-dev \
    libdbus-1-dev \
    libglib2.0-dev \
    libfontconfig1-dev \
    libfreetype6-dev \
    libxml2-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /src
COPY vendor/OrcaSlicer/deps deps
COPY vendor/OrcaSlicer/cmake cmake
COPY vendor/OrcaSlicer/version.inc version.inc

# Skip GUI-only deps that orca-headless doesn't link: GLEW, GLFW, OpenCSG,
# wxWidgets. These would otherwise add ~30–60 min to the build and pull in
# Wayland / X11 / GTK system requirements we don't have. They're leaf deps
# (no other dep depends on them), so removing them is safe.
RUN sed -i \
        -e '/^include(GLEW\/GLEW\.cmake)$/d' \
        -e '/^include(GLFW\/GLFW\.cmake)$/d' \
        -e '/^include(OpenCSG\/OpenCSG\.cmake)$/d' \
        -e '/^include(wxWidgets\/wxWidgets\.cmake)$/d' \
        -e 's/^    dep_GLFW$//' \
        -e 's/^    dep_OpenCSG$//' \
        -e 's/^    \${WXWIDGETS_PKG}$//' \
        deps/CMakeLists.txt

# Build the deps superbuild. This is the long phase — first time can be
# 30–60 minutes depending on the host. The deps tree is self-contained;
# we only need its destdir/ output.
RUN cmake -S deps -B build/deps -G Ninja \
        -DCMAKE_BUILD_TYPE=Release \
        -DDESTDIR=/src/build/destdir && \
    cmake --build build/deps -j"$(nproc)"

# =============================================================================
# Stage 3: Build the orca-headless binary against libslic3r + the deps
# superbuild's destdir.
# =============================================================================
FROM --platform=linux/amd64 ubuntu:24.04 AS cpp-builder

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    cmake \
    git \
    ninja-build \
    pkg-config \
    libssl-dev \
    libgl1-mesa-dev \
    libglu1-mesa-dev \
    libdbus-1-dev \
    libglib2.0-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /src
COPY --from=deps-builder /src/build/destdir /opt/orca-deps
COPY vendor/OrcaSlicer vendor/OrcaSlicer
COPY cpp cpp

# Configure orca-headless using the deps destdir as CMAKE_PREFIX_PATH and
# building libslic3r in-tree via cpp/CMakeLists.txt's add_subdirectory.
RUN cmake -S cpp -B build -G Ninja \
        -DCMAKE_BUILD_TYPE=Release \
        -DCMAKE_PREFIX_PATH=/opt/orca-deps/usr/local \
        -DCMAKE_INSTALL_PREFIX=/opt/orca-headless && \
    cmake --build build -j"$(nproc)" && \
    cmake --install build

# =============================================================================
# Stage 4: Runtime
# =============================================================================
FROM --platform=linux/amd64 ubuntu:24.04

RUN apt-get update && \
    echo 'debconf debconf/frontend select Noninteractive' | debconf-set-selections && \
    apt-get install -y --no-install-recommends \
    python3 \
    python3-pip \
    python3-venv \
    libcurl4t64 \
    libcairo2 \
    libdbus-1-3 \
    libglew2.2 \
    libglu1-mesa \
    libgtk-3-0t64 \
    libsecret-1-0 \
    libmspack0 \
    libsm6 \
    libsoup2.4-1 \
    libssl3t64 \
    libudev1 \
    libwayland-client0 \
    libwayland-egl1 \
    libwebkit2gtk-4.1-0 \
    libxkbcommon0 \
    locales \
    && rm -rf /var/lib/apt/lists/*

ENV LC_ALL=en_US.utf8
RUN locale-gen $LC_ALL
ENV SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt

# Legacy AppImage binary (kept for back-compat in Phase 1)
COPY --from=appimage-extractor /build/squashfs-root/bin/orca-slicer /opt/orcaslicer/bin/orca-slicer
COPY --from=appimage-extractor /build/squashfs-root/resources/ /opt/resources/
COPY --from=appimage-extractor /build/squashfs-root/resources/profiles/ /opt/orcaslicer/profiles/
RUN chmod +x /opt/orcaslicer/bin/orca-slicer

# New orca-headless binary (built from vendored source in cpp-builder stage)
COPY --from=cpp-builder /opt/orca-headless/bin/orca-headless /opt/orca-headless/bin/orca-headless
RUN chmod +x /opt/orca-headless/bin/orca-headless

ENV PATH="/opt/orcaslicer/bin:/opt/orca-headless/bin:${PATH}"

# Install Python dependencies
WORKDIR /app
COPY requirements.txt .
RUN pip3 install --no-cache-dir --break-system-packages -r requirements.txt

COPY requirements-dev.txt /tmp/requirements-dev.txt
RUN pip3 install --no-cache-dir --break-system-packages -r /tmp/requirements-dev.txt

COPY app/ app/
COPY tests/ tests/
COPY conftest.py .

# Bake the source commit hash into the image so the running container can
# log which revision it was built from. Pass via
# ``--build-arg GIT_COMMIT=$(git rev-parse HEAD)`` (docker-compose.yml wires
# this from the host's ``$GIT_COMMIT`` env var). Defaults to ``"unknown"``
# so plain ``docker build`` still works.
ARG GIT_COMMIT=unknown
ENV GIT_COMMIT=${GIT_COMMIT}

EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
