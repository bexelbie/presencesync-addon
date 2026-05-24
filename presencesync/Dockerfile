ARG BUILD_FROM=ghcr.io/home-assistant/amd64-base-python:3.12-alpine3.19

FROM ${BUILD_FROM}

ENV LANG=C.UTF-8 \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# gcompat: glibc compatibility for findmy's local anisette native libraries
RUN apk add --no-cache \
        ca-certificates \
        tzdata \
        git \
        gcompat \
    && pip3 install --no-cache-dir \
        findmy>=0.7.0 \
        pyicloud@git+https://github.com/timlaing/pyicloud.git@180e57c2cbda605ef3c042c3927d7772444676fe \
        paho-mqtt>=2.0 \
        fastapi>=0.110 \
        uvicorn[standard]>=0.27 \
        python-multipart>=0.0.9

COPY rootfs /
RUN chmod +x /usr/src/app/run.sh

WORKDIR /usr/src/app
EXPOSE 8099

CMD ["/usr/src/app/run.sh"]
