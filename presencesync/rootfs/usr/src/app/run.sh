#!/usr/bin/env sh
# Entry point for PresenceSync HA add-on.
# Reads Home Assistant Supervisor options and starts the FastAPI server.
set -e

OPTIONS_FILE=/data/options.json
if [ -f "$OPTIONS_FILE" ]; then
    export PRESENCESYNC_LOG_LEVEL=$(jq -r '.log_level // "info"' "$OPTIONS_FILE")
    export PRESENCESYNC_POLL_INTERVAL=$(jq -r '.poll_interval_seconds // 60' "$OPTIONS_FILE")
    export PRESENCESYNC_ANISETTE_URL=$(jq -r '.anisette_url // ""' "$OPTIONS_FILE")
    export PRESENCESYNC_DISCOVERY_PREFIX=$(jq -r '.mqtt_discovery_prefix // "homeassistant"' "$OPTIONS_FILE")
    export PRESENCESYNC_STATE_PREFIX=$(jq -r '.state_prefix // "presencesync"' "$OPTIONS_FILE")
fi

export PRESENCESYNC_DATA_DIR=/data

# Start embedded Anisette server unless user configured an external one
if [ -z "$PRESENCESYNC_ANISETTE_URL" ] || [ "$PRESENCESYNC_ANISETTE_URL" = "http://localhost:6969" ]; then
    echo "[PresenceSync] Starting embedded Anisette server on :6969"
    export LD_LIBRARY_PATH=/opt/lib:${LD_LIBRARY_PATH:-}
    /usr/local/bin/anisette-v3-server &
    ANISETTE_PID=$!
    export PRESENCESYNC_ANISETTE_URL="http://localhost:6969"
    # Wait briefly for it to start
    sleep 2
    if ! kill -0 "$ANISETTE_PID" 2>/dev/null; then
        echo "[PresenceSync] WARNING: Anisette server failed to start"
    fi
else
    echo "[PresenceSync] Using external Anisette server: $PRESENCESYNC_ANISETTE_URL"
fi

VERSION=$(cd /usr/src/app && python3 -c 'from presencesync import __version__; print(__version__)' 2>/dev/null || echo "?")
echo "[PresenceSync] starting v${VERSION}"
echo "  log_level    = ${PRESENCESYNC_LOG_LEVEL:-info}"
echo "  poll         = ${PRESENCESYNC_POLL_INTERVAL:-60}s"
echo "  anisette     = ${PRESENCESYNC_ANISETTE_URL}"
echo "  data dir     = ${PRESENCESYNC_DATA_DIR}"

exec uvicorn presencesync.web:app \
    --host 0.0.0.0 \
    --port 8099 \
    --log-level "${PRESENCESYNC_LOG_LEVEL:-info}"
