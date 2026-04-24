#!/usr/bin/env bash
##############################################################################
# Harvest Fresh — Quick Sync (incremental, last 25h)
# Fast: completes in minutes. Run this daily/hourly.
##############################################################################
set -euo pipefail
cd "$(dirname "$0")/.."
source .venv-harvester/bin/activate
set -a; source .env.harvester; set +a

PLATFORM="${1:-}"   # optional: ./harvest_fresh.sh workday
WORKERS="${WORKERS:-8}"
MAX="${MAX_COMPANIES:-500}"

echo "▶ Fresh harvest — last 25h | workers=$WORKERS | max=$MAX"
python manage.py harvest_and_push \
  --mode "${MODE:-direct}" \
  ${PLATFORM:+--platform "$PLATFORM"} \
  --since-hours 25 \
  --workers "$WORKERS" \
  --max-companies "$MAX" \
  ${PUSH_URL:+--push-url "$PUSH_URL"} \
  ${PUSH_TOKEN:+--push-token "$PUSH_TOKEN"}
