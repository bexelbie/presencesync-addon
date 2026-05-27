#!/usr/bin/with-contenv sh
# ABOUTME: Entry point for PresenceSync HA add-on.
# ABOUTME: Reads options.json, sets log level, launches uvicorn.
set -e

OPTIONS_FILE=/data/options.json
if [ -f "$OPTIONS_FILE" ]; then
    export PRESENCESYNC_LOG_LEVEL=$(jq -r '.log_level // "info"' "$OPTIONS_FILE")
fi

export PRESENCESYNC_DATA_DIR=/data

VERSION=$(cd /usr/src/app && python3 -c 'from presencesync import __version__; print(__version__)' 2>/dev/null || echo "?")
echo "[PresenceSync] v${VERSION} starting"
echo "  log_level = ${PRESENCESYNC_LOG_LEVEL:-info}"
echo "  data_dir  = ${PRESENCESYNC_DATA_DIR}"

exec uvicorn presencesync.web:app \
    --host 0.0.0.0 \
    --port 8099 \
    --log-level "${PRESENCESYNC_LOG_LEVEL:-info}"
