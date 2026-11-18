#!/usr/bin/env sh
# Start the local phone service that the DSH plugin talks to.
#
# Environment:
#   PHONE_BIN            path to the `phone` CLI (default: .venv/bin/phone)
#   HAND_HOST            ESP32-S3 hand host or IP (default: hand.local)
#   PHONE_PORT           HTTP port (default: 8765)
#   PHONE_SERVICE_TOKEN  bearer token; when set, requests must send it
#   EYE_MODE             scrcpy | adb | screenrecord | ios (default: scrcpy)
#
# Examples:
#   scripts/serve_phone.sh
#   HAND_HOST=192.168.1.20 PHONE_SERVICE_TOKEN=secret scripts/serve_phone.sh
set -eu

PHONE_BIN="${PHONE_BIN:-.venv/bin/phone}"
HAND_HOST="${HAND_HOST:-hand.local}"
PHONE_PORT="${PHONE_PORT:-8765}"
EYE_MODE="${EYE_MODE:-scrcpy}"

case "$EYE_MODE" in
  scrcpy|adb|screenrecord|ios) ;;
  *) echo "unknown EYE_MODE: $EYE_MODE" >&2; exit 2 ;;
esac

AUTH_ARGS=""
if [ -n "${PHONE_SERVICE_TOKEN:-}" ]; then
  AUTH_ARGS="--token ${PHONE_SERVICE_TOKEN}"
fi

# shellcheck disable=SC2086
exec "$PHONE_BIN" --tcp "$HAND_HOST" serve "--${EYE_MODE}" \
  --http-port "$PHONE_PORT" $AUTH_ARGS
