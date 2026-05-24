# PresenceSync Addon — Multi-stage Dockerfile
# Stage 1: Build export-findmy (Rust)
# Stage 2: Get anisette-v3-server from its Docker image
# Stage 3: Final image (Debian + Python + binaries)
ARG BUILD_FROM=ghcr.io/home-assistant/amd64-base-debian:bookworm

# ─── Stage 1: Rust builder ───────────────────────────────────────────────────
FROM rust:1.86-bookworm AS rust-builder

# Install nightly toolchain (needed for latest crate MSRVs)
RUN rustup toolchain install nightly && rustup default nightly

RUN apt-get update && apt-get install -y --no-install-recommends \
    pkg-config libssl-dev git ca-certificates protobuf-compiler \
    && rm -rf /var/lib/apt/lists/*

# Clone our patched repos (no --recursive since we replace apple-private-apis)
RUN git clone --branch presencesync --depth 1 \
    https://github.com/bexelbie/rustpush.git /build/rustpush-local \
    && cd /build/rustpush-local \
    && git submodule update --init open-absinthe

# Clone apple-private-apis fork with omnisette patches
RUN rm -rf /build/rustpush-local/apple-private-apis \
    && git clone --branch presencesync --depth 1 \
    https://github.com/bexelbie/apple-private-apis.git /build/rustpush-local/apple-private-apis

# Create stub clearadi crate (optional dep, not used, but cargo resolves all path deps)
RUN mkdir -p /build/rustpush-local/apple-private-apis/clearadi/src \
    && printf '[package]\nname = "clearadi"\nversion = "1.0.0"\nedition = "2021"\n\n[dependencies]\nserde = { version = "1", features = ["derive"] }\n' \
       > /build/rustpush-local/apple-private-apis/clearadi/Cargo.toml \
    && echo '' > /build/rustpush-local/apple-private-apis/clearadi/src/lib.rs

RUN git clone --depth 1 \
    https://github.com/bexelbie/export-findmy.git /build/export-findmy

# Rewrite Cargo.toml to use local paths for build
WORKDIR /build/export-findmy
RUN cat > Cargo.toml <<'EOF'
[package]
name = "export-findmy"
version = "0.1.0"
edition = "2021"

[dependencies]
rustpush = { path = "../rustpush-local", default-features = false, features = ["remote-anisette-v3"] }
omnisette = { path = "../rustpush-local/apple-private-apis/omnisette" }
icloud_auth = { path = "../rustpush-local/apple-private-apis/icloud-auth", default-features = false, features = ["remote-anisette-v3"] }
keystore = { path = "../rustpush-local/keystore" }
cloudkit-proto = { path = "../rustpush-local/cloudkit-proto" }
tokio = { version = "1", features = ["full"] }
plist = "1.7.0"
sha2 = "0.10.8"
async-trait = "0.1.73"
uuid = { version = "1.4.1", features = ["v4"] }
rand = "0.8.5"
libc = "0.2"
pretty_env_logger = "0.5.0"
serde = { version = "1", features = ["derive"] }
serde_json = "1"
EOF

# Build release binary
RUN cargo build --release \
    && cp target/release/export-findmy /usr/local/bin/export-findmy \
    && strip /usr/local/bin/export-findmy

# ─── Stage 2: Extract anisette binary from its Docker image ──────────────────
FROM dadoum/anisette-v3-server:latest AS anisette-source

# ─── Stage 3: Final image ────────────────────────────────────────────────────
FROM ${BUILD_FROM}

# Target architecture (set by HA builder: amd64, aarch64)
ARG BUILD_ARCH=amd64

ENV LANG=C.UTF-8 \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# Install Python + runtime deps
RUN apt-get update && apt-get install -y --no-install-recommends \
        python3 python3-pip python3-venv \
        ca-certificates curl git jq tzdata \
        libssl3 zlib1g \
    && rm -rf /var/lib/apt/lists/*

# Install Python packages
RUN pip3 install --no-cache-dir --break-system-packages \
    findmy>=0.7.0 \
    "pyicloud @ git+https://github.com/timlaing/pyicloud.git@180e57c2cbda605ef3c042c3927d7772444676fe" \
    paho-mqtt>=2.0 \
    "fastapi>=0.110" \
    "uvicorn[standard]>=0.27" \
    python-multipart>=0.0.9 \
    cryptography>=42.0 \
    requests>=2.31 \
    aiohttp>=3.9

# Copy export-findmy binary from rust builder
COPY --from=rust-builder /usr/local/bin/export-findmy /usr/local/bin/export-findmy

# Copy anisette-v3-server binary from its image
COPY --from=anisette-source /opt/anisette-v3-server /usr/local/bin/anisette-v3-server
RUN chmod +x /usr/local/bin/anisette-v3-server

# Copy application code
COPY rootfs /
RUN chmod +x /usr/src/app/run.sh

WORKDIR /usr/src/app
EXPOSE 8099

CMD ["/usr/src/app/run.sh"]
