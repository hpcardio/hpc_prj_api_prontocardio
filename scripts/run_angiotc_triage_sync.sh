#!/usr/bin/env sh
set -eu

APP_DIR="${APP_DIR:-/home/admpronto/hpc_projetos/api-prontocardio}"
CONTAINER="${API_PRONTOCARDIO_CONTAINER:-api-prontocardio-api_prontocardio-1}"
SCRIPT_PATH="$APP_DIR/scripts/sync_angiotc_triage_to_nvc.py"

cd "$APP_DIR"

if [ -f .env ]; then
  set -a
  # shellcheck disable=SC1091
  . ./.env
  set +a
fi

docker exec -i \
  -e ANGIOTC_IMPORT_TOKEN="${ANGIOTC_IMPORT_TOKEN:-}" \
  -e NVC_ANGIOTC_TRIAGE_URL="${NVC_ANGIOTC_TRIAGE_URL:-http://192.168.4.45:4003/api/angiotc/triage-entry}" \
  -e ANGIOTC_MV_QUEUE_IDS="${ANGIOTC_MV_QUEUE_IDS:-8}" \
  -e ANGIOTC_TRIAGE_LOOKBACK_MINUTES="${ANGIOTC_TRIAGE_LOOKBACK_MINUTES:-45}" \
  "$CONTAINER" python - < "$SCRIPT_PATH"
