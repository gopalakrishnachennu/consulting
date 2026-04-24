#!/usr/bin/env bash
##############################################################################
# Harvest Full — Full Crawl (EVERY job from EVERY company)
# Slow: 1-3 hours. Run weekly or after adding new companies.
# Fetches all JDs + enriches (skills, tech_stack, salary, visa, quality_score).
##############################################################################
set -euo pipefail
cd "$(dirname "$0")/.."
source .venv-harvester/bin/activate
set -a; source .env.harvester; set +a

PLATFORM="${1:-}"
WORKERS="${WORKERS:-12}"      # higher than fresh — we have time
MAX="${MAX_COMPANIES:-2000}"  # get everything

echo "▶ FULL CRAWL — all jobs + JDs + metadata | workers=$WORKERS | max=$MAX"
echo "  (this may take 1-3 hours depending on company count)"
python manage.py harvest_and_push \
  --mode "${MODE:-direct}" \
  ${PLATFORM:+--platform "$PLATFORM"} \
  --fetch-all \
  --workers "$WORKERS" \
  --max-companies "$MAX" \
  --batch-size 500 \
  ${PUSH_URL:+--push-url "$PUSH_URL"} \
  ${PUSH_TOKEN:+--push-token "$PUSH_TOKEN"}
