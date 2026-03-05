# Stage 1: Build OrcaSlicer from source
FROM ubuntu:24.04 AS builder

RUN apt-get update && \
    echo 'debconf debconf/frontend select Noninteractive' | debconf-set-selections

RUN apt-get update && apt-get install -y \
    autoconf \
    build-essential \
    cmake \
    curl \
    eglexternalplatform-dev \
    extra-cmake-modules \
    file \
    git \
    gstreamer1.0-plugins-bad \
    gstreamer1.0-libav \
    libcairo2-dev \
    libcurl4-openssl-dev \
    libdbus-1-dev \
    libglew-dev \
    libglu1-mesa-dev \
    libgstreamer1.0-dev \
    libgstreamerd-3-dev \
    libgstreamer-plugins-base1.0-dev \
    libgstreamer-plugins-good1.0-dev \
    libgtk-3-dev \
    libsecret-1-dev \
    libsoup2.4-dev \
    libssl3 \
    libssl-dev \
    libtool \
    libudev-dev \
    libwayland-dev \
    libwebkit2gtk-4.1-dev \
    libxkbcommon-dev \
    locales \
    locales-all \
    m4 \
    pkgconf \
    sudo \
    wayland-protocols \
    wget

ENV LC_ALL=en_US.utf8
RUN locale-gen $LC_ALL

WORKDIR /build
RUN git clone --depth 1 --branch v2.3.1 https://github.com/SoftFever/OrcaSlicer.git

WORKDIR /build/OrcaSlicer
RUN ./build_linux.sh -u
RUN ./build_linux.sh -dr
RUN ./build_linux.sh -sr

# Stage 2: Runtime
FROM ubuntu:24.04

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

# Copy OrcaSlicer binary and libraries
COPY --from=builder /build/OrcaSlicer/build/package/ /opt/orcaslicer/

# Copy BBL profiles
COPY --from=builder /build/OrcaSlicer/resources/profiles/BBL.json /opt/orcaslicer/profiles/BBL.json
COPY --from=builder /build/OrcaSlicer/resources/profiles/BBL/ /opt/orcaslicer/profiles/BBL/

# Make binary executable and add to PATH
RUN chmod +x /opt/orcaslicer/bin/orca-slicer
ENV PATH="/opt/orcaslicer/bin:${PATH}"

# Install Python dependencies
WORKDIR /app
COPY requirements.txt .
RUN pip3 install --no-cache-dir --break-system-packages -r requirements.txt

COPY app/ app/

EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
