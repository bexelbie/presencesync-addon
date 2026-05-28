# ABOUTME: Builds the PresenceSync Home Assistant add-on image.
# ABOUTME: Copies binary artifacts from pinned container images into the runtime image.
ARG BUILD_FROM=ghcr.io/home-assistant/amd64-base-debian:bookworm
ARG EXPORT_FINDMY_IMAGE=ghcr.io/bexelbie/export-findmy:presencesync@sha256:ed22a07341f45bffaae2df76b51e479b624526caf1b5778b9583d6616a6236d5

FROM ${EXPORT_FINDMY_IMAGE} AS export-findmy-source

FROM dadoum/anisette-v3-server@sha256:1e20384985d3c49965f444bef39d627768dacc39ea0dca91f2a535edb7591ba3 AS anisette-source

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
    findmy==0.10.0 \
    "pyicloud @ git+https://github.com/timlaing/pyicloud.git@180e57c2cbda605ef3c042c3927d7772444676fe" \
    paho-mqtt>=2.0 \
    "fastapi>=0.110" \
    "uvicorn[standard]>=0.27" \
    python-multipart>=0.0.9 \
    cryptography>=42.0 \
    requests>=2.31 \
    aiohttp>=3.9

COPY --from=export-findmy-source /usr/local/bin/export-findmy /usr/local/bin/export-findmy

COPY --from=anisette-source /opt/anisette-v3-server /usr/local/bin/anisette-v3-server
RUN chmod +x /usr/local/bin/anisette-v3-server

# Copy application code
COPY rootfs /
RUN chmod +x /usr/src/app/run.sh

WORKDIR /usr/src/app
EXPOSE 8099

CMD ["/usr/src/app/run.sh"]
