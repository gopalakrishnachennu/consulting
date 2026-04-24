#!/usr/bin/env bash
##############################################################################
# Harvest Status — check prod DB counts (local vs prod consistency)
# Shows: total RawJobs, pending/synced, breakdown by platform.
##############################################################################
set -euo pipefail
cd "$(dirname "$0")/.."
[ -f .env.harvester ] && { set -a; source .env.harvester; set +a; }

URL="${PUSH_URL:-https://chennu.co}"
TOKEN="${PUSH_TOKEN:-${HARVEST_PUSH_SECRET:-}}"

if [ -z "$TOKEN" ]; then
  echo "ERROR: set PUSH_TOKEN (or HARVEST_PUSH_SECRET) in .env.harvester" >&2
  exit 1
fi

echo "▶ Prod status @ $URL"
curl -sS -H "Authorization: Bearer $TOKEN" "$URL/harvest/api/push/status/" \
  | python3 -m json.tool
