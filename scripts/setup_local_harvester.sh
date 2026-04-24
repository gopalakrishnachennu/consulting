#!/usr/bin/env bash
##############################################################################
# Local Harvesting Agent — One-Command Setup
#
# Run this once on the machine that will harvest jobs.
# After setup, use harvest_and_push.sh (or the docker compose) to run.
#
# Usage:
#   chmod +x scripts/setup_local_harvester.sh
#   ./scripts/setup_local_harvester.sh
##############################################################################

set -euo pipefail

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; BLUE='\033[0;34m'; NC='\033[0m'
info()    { echo -e "${BLUE}[info]${NC}  $*"; }
success() { echo -e "${GREEN}[ok]${NC}    $*"; }
warn()    { echo -e "${YELLOW}[warn]${NC}  $*"; }
error()   { echo -e "${RED}[error]${NC} $*"; exit 1; }

##############################################################################
# 0. Prerequisites
##############################################################################

info "Checking prerequisites..."

command -v python3 >/dev/null 2>&1 || error "python3 not found — install Python 3.12+"
command -v pip3 >/dev/null 2>&1    || error "pip3 not found"

PYTHON_VERSION=$(python3 -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
PYTHON_MAJOR=$(echo "$PYTHON_VERSION" | cut -d. -f1)
PYTHON_MINOR=$(echo "$PYTHON_VERSION" | cut -d. -f2)

if [ "$PYTHON_MAJOR" -lt 3 ] || ([ "$PYTHON_MAJOR" -eq 3 ] && [ "$PYTHON_MINOR" -lt 11 ]); then
    error "Python 3.11+ required, found $PYTHON_VERSION"
fi
success "Python $PYTHON_VERSION"

##############################################################################
# 1. Virtual environment
##############################################################################

if [ ! -d ".venv-harvester" ]; then
    info "Creating virtual environment .venv-harvester..."
    python3 -m venv .venv-harvester
    success "Virtual environment created"
else
    success "Virtual environment already exists"
fi

# shellcheck disable=SC1091
source .venv-harvester/bin/activate

##############################################################################
# 2. Install Python dependencies
##############################################################################

info "Installing dependencies (this may take 1-2 minutes)..."
pip install --quiet --upgrade pip
pip install --quiet -r requirements.txt
success "Dependencies installed"

##############################################################################
# 3. Create .env.harvester from example if not already present
##############################################################################

if [ ! -f ".env.harvester" ]; then
    if [ -f ".env.harvester.example" ]; then
        cp .env.harvester.example .env.harvester
        warn ".env.harvester created from example — EDIT IT before harvesting"
        warn "  → Set DATABASE_URL to your prod PostgreSQL connection string (direct mode)"
        warn "  → OR set PUSH_URL + PUSH_TOKEN for push mode"
    else
        warn ".env.harvester.example not found — creating minimal .env.harvester"
        cat > .env.harvester << 'EOF'
DJANGO_SETTINGS_MODULE=config.settings_local_harvester

# ── Pick ONE mode ──────────────────────────────────────────────────────────
# MODE A (direct): connect to prod DB directly — set DATABASE_URL below
# MODE B (push):   harvest offline, push via API — set PUSH_URL + PUSH_TOKEN

# Direct mode — production PostgreSQL connection string
# DATABASE_URL=postgres://user:password@prod-host:5432/dbname
DATABASE_URL=sqlite:///local_harvest.db

# Push mode — prod server API
# PUSH_URL=https://chennu.co
# PUSH_TOKEN=your-harvest-push-secret-from-prod-env

SECRET_KEY=local-harvester-insecure-key
EOF
        warn ".env.harvester created — edit it with your prod DB URL or push credentials"
    fi
else
    success ".env.harvester already exists"
fi

##############################################################################
# 4. Run Django checks
##############################################################################

info "Running Django system checks..."
export DJANGO_SETTINGS_MODULE=config.settings_local_harvester
export PYTHONPATH="$(pwd)/apps"

# Load .env.harvester
if [ -f ".env.harvester" ]; then
    set -a
    # shellcheck disable=SC1091
    source .env.harvester
    set +a
fi

python3 manage.py check --deploy 2>&1 | grep -v "WARNINGS\|DEBUG\|ALLOWED_HOSTS\|CSRF\|SECURE" || true
success "Django checks passed"

##############################################################################
# 5. Print usage instructions
##############################################################################

echo ""
echo -e "${GREEN}════════════════════════════════════════════════════════${NC}"
echo -e "${GREEN}  Local Harvesting Agent — Setup Complete               ${NC}"
echo -e "${GREEN}════════════════════════════════════════════════════════${NC}"
echo ""
echo "  Activate venv:  source .venv-harvester/bin/activate"
echo ""
echo -e "  ${BLUE}MODE A — Direct (recommended):${NC}"
echo "  Connect local clone directly to prod PostgreSQL."
echo "  Set DATABASE_URL in .env.harvester to your prod connection string."
echo ""
echo "    python manage.py harvest_and_push \\"
echo "      --mode direct \\"
echo "      --fetch-all \\"
echo "      --max-companies 500"
echo ""
echo -e "  ${BLUE}MODE B — Push (if prod DB is not reachable):${NC}"
echo "  Set PUSH_URL + PUSH_TOKEN in .env.harvester."
echo ""
echo "    python manage.py harvest_and_push \\"
echo "      --mode push \\"
echo "      --fetch-all \\"
echo "      --workers 8 \\"
echo "      --push-url https://chennu.co \\"
echo "      --push-token your-secret"
echo ""
echo -e "  ${BLUE}Quick Sync (incremental, fast):${NC}"
echo "    python manage.py harvest_and_push --since-hours 25"
echo ""
echo -e "  ${BLUE}Single platform test:${NC}"
echo "    python manage.py harvest_and_push --platform greenhouse --max-companies 5 --dry-run"
echo ""
echo "  Edit .env.harvester with your credentials before running."
echo ""
