ARG BUILD_FROM=ghcr.io/home-assistant/amd64-base-python:3.12-alpine3.19

# ── Stage 1: build anisette-v3-server from source (works on all architectures) ──
FROM debian:stable-slim AS anisette-builder
RUN apt-get update && apt-get install --no-install-recommends -y \
        ca-certificates ldc git clang dub libz-dev libssl-dev libplist-dev \
    && apt-get clean && rm -rf /var/lib/apt/lists/*
WORKDIR /build
RUN git clone --depth 1 https://github.com/Dadoum/anisette-v3-server.git . \
    && DC=ldc2 dub build -c "static" --build-mode allAtOnce -b release --compiler=ldc2

# ── Stage 2: final image ──
FROM ${BUILD_FROM}

ENV LANG=C.UTF-8 \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# Copy anisette binary built for the target architecture
COPY --from=anisette-builder /build/anisette-v3-server /usr/local/bin/anisette-v3-server

# Install runtime deps — findmy requires cryptography (already in base), aiohttp, etc.
RUN apk add --no-cache \
        ca-certificates \
        tzdata \
        git \
        libstdc++ \
    && pip3 install --no-cache-dir \
        findmy>=0.7.0 \
        pyicloud@git+https://github.com/timlaing/pyicloud.git@180e57c2cbda605ef3c042c3927d7772444676fe \
        paho-mqtt>=2.0 \
        fastapi>=0.110 \
        uvicorn[standard]>=0.27 \
        jinja2>=3.1 \
        python-multipart>=0.0.9 \
        aiofiles>=23.2 \
        pyyaml>=6.0

# Copy the application
COPY rootfs /
RUN chmod +x /usr/src/app/run.sh

WORKDIR /usr/src/app
EXPOSE 8099

CMD ["/usr/src/app/run.sh"]
