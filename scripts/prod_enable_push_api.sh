#!/usr/bin/env bash
##############################################################################
#  prod_enable_push_api.sh — ONE command to enable the push API on PROD.
#
#  Run this ONCE on the prod server (62.238.6.14) after deploying this branch.
#
#  What it does:
#    1. Generates HARVEST_PUSH_SECRET (64-hex random token)
#    2. Appends it to the prod .env file (skips if already present)
#    3. Restarts the web container so it picks up the new setting
#    4. Prints the secret — paste it into .env.harvester on your local machine
#
#  Usage (on prod server):
#    cd /path/to/consulting
#    ./scripts/prod_enable_push_api.sh
##############################################################################
set -euo pipefail
cd "$(dirname "$0")/.."

G='\033[0;32m'; Y='\033[1;33m'; R='\033[0;31m'; N='\033[0m'
ok()   { echo -e "${G}✓${N} $*"; }
warn() { echo -e "${Y}⚠${N} $*"; }
err()  { echo -e "${R}✗${N} $*" >&2; exit 1; }

ENV_FILE=".env"
[ -f "$ENV_FILE" ] || err ".env not found in $(pwd) — run from project root"

# 1. Check if already set
if grep -q "^HARVEST_PUSH_SECRET=" "$ENV_FILE"; then
    EXISTING=$(grep "^HARVEST_PUSH_SECRET=" "$ENV_FILE" | cut -d= -f2-)
    warn "HARVEST_PUSH_SECRET already set in $ENV_FILE"
    echo
    echo "    Token: $EXISTING"
    echo
    echo "  Use this same value as PUSH_TOKEN in your local .env.harvester"
    exit 0
fi

# 2. Generate secret
SECRET=$(python3 -c "import secrets; print(secrets.token_hex(32))")
echo "" >> "$ENV_FILE"
echo "# Local Harvesting Agent — bearer token for /harvest/api/push/*" >> "$ENV_FILE"
echo "HARVEST_PUSH_SECRET=$SECRET" >> "$ENV_FILE"
ok "Added HARVEST_PUSH_SECRET to $ENV_FILE"

# 3. Restart web container (best-effort — try common compose files)
COMPOSE_FILE=""
for f in docker-compose.prod.yml docker-compose.yml; do
    [ -f "$f" ] && COMPOSE_FILE="$f" && break
done

if [ -n "$COMPOSE_FILE" ] && command -v docker >/dev/null; then
    echo
    echo "Restarting web service via $COMPOSE_FILE..."
    docker compose -f "$COMPOSE_FILE" up -d --no-deps web || \
        warn "Could not auto-restart — restart web container manually to load the new env"
else
    warn "No docker-compose file found — restart web service manually"
fi

# 4. Show result + verify
echo
echo -e "${G}═══════════════════════════════════════════════════════════════${N}"
echo -e "${G}  Push API enabled. Save this token:${N}"
echo
echo "    HARVEST_PUSH_SECRET=$SECRET"
echo
echo -e "${G}  Paste it into your LOCAL .env.harvester as:${N}"
echo
echo "    PUSH_TOKEN=$SECRET"
echo -e "${G}═══════════════════════════════════════════════════════════════${N}"
echo
echo "Verify from your local machine with:"
echo "    curl -H 'Authorization: Bearer $SECRET' https://chennu.co/harvest/api/push/status/"
echo
