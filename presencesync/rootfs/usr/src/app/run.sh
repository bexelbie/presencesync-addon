#!/usr/bin/env sh
# Entry point for PresenceSync HA add-on.
# Reads Home Assistant Supervisor options and starts the FastAPI server.
set -e

OPTIONS_FILE=/data/options.json
if [ -f "$OPTIONS_FILE" ]; then
    export PRESENCESYNC_LOG_LEVEL=$(jq -r '.log_level // "info"' "$OPTIONS_FILE")
    export PRESENCESYNC_POLL_INTERVAL=$(jq -r '.poll_interval_seconds // 60' "$OPTIONS_FILE")
    export PRESENCESYNC_ANISETTE_URL=$(jq -r '.anisette_url // "http://homeassistant.local:6969"' "$OPTIONS_FILE")
    export PRESENCESYNC_DISCOVERY_PREFIX=$(jq -r '.mqtt_discovery_prefix // "homeassistant"' "$OPTIONS_FILE")
    export PRESENCESYNC_STATE_PREFIX=$(jq -r '.state_prefix // "presencesync"' "$OPTIONS_FILE")
fi

export PRESENCESYNC_DATA_DIR=/data

VERSION=$(cd /usr/src/app && python3 -c 'from presencesync import __version__; print(__version__)' 2>/dev/null || echo "?")
echo "[PresenceSync] starting v${VERSION}"
echo "  log_level    = ${PRESENCESYNC_LOG_LEVEL:-info}"
echo "  poll         = ${PRESENCESYNC_POLL_INTERVAL:-60}s"
echo "  anisette     = ${PRESENCESYNC_ANISETTE_URL:-(unset)}"
echo "  data dir     = ${PRESENCESYNC_DATA_DIR}"

exec uvicorn presencesync.web:app \
    --host 0.0.0.0 \
    --port 8099 \
    --log-level "${PRESENCESYNC_LOG_LEVEL:-info}"
