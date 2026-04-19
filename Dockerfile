# Stage 1: Extract pre-built OrcaSlicer from AppImage
FROM --platform=linux/amd64 ubuntu:24.04 AS builder

RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates wget squashfs-tools && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /build

# Download AppImage
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

# Stage 2: Runtime
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

# Copy OrcaSlicer binary and profiles from extracted AppImage
COPY --from=builder /build/squashfs-root/bin/orca-slicer /opt/orcaslicer/bin/orca-slicer
COPY --from=builder /build/squashfs-root/resources/profiles/ /opt/orcaslicer/profiles/

# Make binary executable and add to PATH
RUN chmod +x /opt/orcaslicer/bin/orca-slicer
ENV PATH="/opt/orcaslicer/bin:${PATH}"

# Install Python dependencies
WORKDIR /app
COPY requirements.txt .
RUN pip3 install --no-cache-dir --break-system-packages -r requirements.txt

COPY requirements-dev.txt /tmp/requirements-dev.txt
RUN pip3 install --no-cache-dir --break-system-packages -r /tmp/requirements-dev.txt

COPY app/ app/
COPY tests/ tests/
COPY conftest.py .

EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
