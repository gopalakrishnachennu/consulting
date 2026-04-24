#!/usr/bin/env bash
##############################################################################
#  harvest.sh — ONE command to harvest + push everything.
#
#  First run:   ./scripts/harvest.sh        # auto-setup venv, deps, .env
#  Every run:   ./scripts/harvest.sh        # fresh jobs (last 25h)
#  Full crawl:  ./scripts/harvest.sh full   # all jobs + JDs from all platforms
#  One portal:  ./scripts/harvest.sh full workday
#  Status:      ./scripts/harvest.sh status # check prod RawJob counts
#
#  Duplicates are auto-prevented by url_hash (SHA256) unique constraint.
#  Safe to re-run anytime — already-synced jobs are skipped.
##############################################################################
set -euo pipefail

# ── paths & colours ──────────────────────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$ROOT"

G='\033[0;32m'; Y='\033[1;33m'; R='\033[0;31m'; B='\033[0;34m'; N='\033[0m'
ok()   { echo -e "${G}✓${N} $*"; }
info() { echo -e "${B}▶${N} $*"; }
warn() { echo -e "${Y}⚠${N} $*"; }
err()  { echo -e "${R}✗${N} $*" >&2; exit 1; }

MODE_ARG="${1:-fresh}"
PLATFORM="${2:-}"

# ── 1. Auto-setup on first run ───────────────────────────────────────────────
if [ ! -d ".venv-harvester" ]; then
    info "First run detected — setting up venv & deps (~2 min)"
    command -v python3 >/dev/null || err "python3 not found"
    python3 -m venv .venv-harvester
    # shellcheck disable=SC1091
    source .venv-harvester/bin/activate
    pip install --quiet --upgrade pip
    pip install --quiet -r requirements.txt
    ok "venv ready"
else
    # shellcheck disable=SC1091
    source .venv-harvester/bin/activate
fi

# ── 2. Auto-create .env.harvester if missing ─────────────────────────────────
if [ ! -f ".env.harvester" ]; then
    warn ".env.harvester missing — creating template"
    cat > .env.harvester <<'EOF'
# ═══════════════════════════════════════════════════════════════════════════
# LOCAL HARVESTING AGENT — fill in ONE of the two modes below, then run:
#   ./scripts/harvest.sh
# ═══════════════════════════════════════════════════════════════════════════

DJANGO_SETTINGS_MODULE=config.settings_local_harvester
SECRET_KEY=local-harvester-change-me

# ───── MODE A: DIRECT (recommended — fastest) ─────────────────────────────
# Point to your PROD PostgreSQL. Get password from the prod .env file.
# Host: 62.238.6.14   DB: consulting   User: consulting
MODE=direct
DATABASE_URL=postgres://consulting:PASTE_PROD_DB_PASSWORD_HERE@62.238.6.14:5432/consulting

# ───── MODE B: PUSH (uncomment if prod DB not reachable) ──────────────────
# MODE=push
# DATABASE_URL=sqlite:///local_harvest.db
# PUSH_URL=https://chennu.co
# PUSH_TOKEN=PASTE_HARVEST_PUSH_SECRET_FROM_PROD

# Local machine has no hosting limits — go wild:
JARVIS_HTTP_MAX_GLOBAL=500
JARVIS_HTTP_MAX_PER_HOST=20
HARVEST_BACKFILL_INTER_JOB_DELAY_SEC=0.02
CELERY_BROKER_URL=memory://
CELERY_RESULT_BACKEND=cache+memory://
EOF
    warn "Edit .env.harvester → set DATABASE_URL or PUSH_URL+PUSH_TOKEN, then re-run"
    exit 1
fi

# ── 3. Load env ──────────────────────────────────────────────────────────────
set -a; source .env.harvester; set +a
: "${MODE:=direct}"

# Validate mode config
if [ "$MODE" = "direct" ]; then
    if [[ "$DATABASE_URL" == *"PASTE_PROD_DB_PASSWORD"* ]]; then
        err "Edit .env.harvester and set DATABASE_URL to your real prod password"
    fi
    CREDS_HINT="DATABASE_URL=$(echo "$DATABASE_URL" | sed 's|://[^@]*@|://***@|')"
elif [ "$MODE" = "push" ]; then
    [ -z "${PUSH_URL:-}" ]   && err "MODE=push requires PUSH_URL in .env.harvester"
    [ -z "${PUSH_TOKEN:-}" ] && err "MODE=push requires PUSH_TOKEN in .env.harvester"
    CREDS_HINT="PUSH_URL=$PUSH_URL"
else
    err "MODE must be 'direct' or 'push' (got: $MODE)"
fi

# ── 4. Status-only shortcut ──────────────────────────────────────────────────
if [ "$MODE_ARG" = "status" ]; then
    URL="${PUSH_URL:-https://chennu.co}"
    TOKEN="${PUSH_TOKEN:-${HARVEST_PUSH_SECRET:-}}"
    [ -z "$TOKEN" ] && err "Need PUSH_TOKEN in .env.harvester to check status"
    info "Prod status @ $URL"
    curl -sS -H "Authorization: Bearer $TOKEN" "$URL/harvest/api/push/status/" \
        | python3 -m json.tool
    exit 0
fi

# ── 5. Decide run parameters ─────────────────────────────────────────────────
if [ "$MODE_ARG" = "full" ]; then
    FLAGS=(--fetch-all --max-companies 2000 --workers 12 --batch-size 500)
    LABEL="FULL CRAWL — every job + every JD + all metadata"
else
    FLAGS=(--since-hours 25 --max-companies 500 --workers 8 --batch-size 500)
    LABEL="FRESH — jobs updated in last 25 hours"
fi
[ -n "$PLATFORM" ] && FLAGS+=(--platform "$PLATFORM")

# ── 6. Run it ────────────────────────────────────────────────────────────────
echo
info "$LABEL"
info "Mode: $MODE | $CREDS_HINT${PLATFORM:+ | platform=$PLATFORM}"
info "Dedup: url_hash unique constraint — duplicates auto-skipped"
echo

START=$(date +%s)

python manage.py harvest_and_push --mode "$MODE" "${FLAGS[@]}" \
    ${PUSH_URL:+--push-url "$PUSH_URL"} \
    ${PUSH_TOKEN:+--push-token "$PUSH_TOKEN"}

ELAPSED=$(( $(date +%s) - START ))
echo
ok "Finished in ${ELAPSED}s"

# ── 7. Post-run consistency check ────────────────────────────────────────────
if [ "$MODE" = "push" ] && [ -n "${PUSH_TOKEN:-}" ]; then
    echo
    info "Prod status:"
    curl -sS -H "Authorization: Bearer $PUSH_TOKEN" \
        "$PUSH_URL/harvest/api/push/status/" \
        | python3 -c "
import json, sys
d = json.load(sys.stdin)
print(f\"  total:   {d['total']:>8,}\")
print(f\"  pending: {d['pending']:>8,}  (waiting for sync → pool)\")
print(f\"  synced:  {d['synced']:>8,}  (already in pool/live)\")
print(f\"  skipped: {d['skipped']:>8,}  (duplicates blocked)\")
"
elif [ "$MODE" = "direct" ]; then
    echo
    info "RawJob counts (direct — reading prod DB):"
    python3 -c "
import os, django
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings_local_harvester')
django.setup()
from harvest.models import RawJob
print(f\"  total:   {RawJob.objects.count():>8,}\")
print(f\"  pending: {RawJob.objects.filter(sync_status='PENDING').count():>8,}\")
print(f\"  synced:  {RawJob.objects.filter(sync_status='SYNCED').count():>8,}\")
"
fi

echo
ok "Done. Prod pipeline (vetting → live jobs) is now processing new rows."
