#!/usr/bin/env bash
# One-shot: push to GitHub, then SSH to production and rebuild containers.
#
# Usage (one command):
#   ./deploy.sh YOUR_SERVER_IP
#
# Or set env / .env.deploy:
#   export DEPLOY_HOST=your.server.ip
#   export DEPLOY_USER=root          # optional, default root
#   export DEPLOY_PATH=/opt/consulting
#   export DEPLOY_BRANCH=main
#   ./deploy.sh
#
# Optional:
#   DEPLOY_SSH_KEY=~/.ssh/id_ed25519
#   DEPLOY_SLEEP=3                   # seconds between major steps (timelapse pacing)
#   ALLOW_DIRTY=1                    # allow deploy with uncommitted changes
#   REQUIRE_MAIN=0                   # allow deploying when not on main
#   DRY_RUN=1                        # print commands only
#
# You can also create .env.deploy (same variables, sourced automatically):
#   DEPLOY_HOST=1.2.3.4

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

if [[ -f "$ROOT/.env.deploy" ]]; then
  # shellcheck source=/dev/null
  set -a && source "$ROOT/.env.deploy" && set +a
fi

# First argument = server host (overrides DEPLOY_HOST from env / .env.deploy)
if [[ -n "${1:-}" ]]; then
  DEPLOY_HOST="$1"
fi

DEPLOY_USER="${DEPLOY_USER:-root}"
DEPLOY_PATH="${DEPLOY_PATH:-/opt/consulting}"
DEPLOY_BRANCH="${DEPLOY_BRANCH:-main}"
DEPLOY_SLEEP="${DEPLOY_SLEEP:-3}"
ALLOW_DIRTY="${ALLOW_DIRTY:-0}"
REQUIRE_MAIN="${REQUIRE_MAIN:-1}"
DRY_RUN="${DRY_RUN:-0}"

ts() { date -u +"%Y-%m-%dT%H:%M:%SZ"; }
log() { echo "[$(ts)] $*"; }
pause() {
  local s="${1:-$DEPLOY_SLEEP}"
  log "— pause ${s}s —"
  sleep "$s"
}

if [[ -z "${DEPLOY_HOST:-}" ]]; then
  log "ERROR: Set DEPLOY_HOST (e.g. export DEPLOY_HOST=203.0.113.10) or add it to .env.deploy"
  exit 1
fi

SSH_OPTS=(-o BatchMode=yes -o StrictHostKeyChecking=accept-new)
if [[ -n "${DEPLOY_SSH_KEY:-}" ]]; then
  SSH_OPTS+=(-i "$DEPLOY_SSH_KEY")
fi

run() {
  if [[ "$DRY_RUN" == "1" ]]; then
    echo "[dry-run] $*"
  else
    "$@"
  fi
}

CURRENT_BRANCH="$(git branch --show-current 2>/dev/null || true)"
if [[ "$REQUIRE_MAIN" == "1" && -n "$CURRENT_BRANCH" && "$CURRENT_BRANCH" != "$DEPLOY_BRANCH" ]]; then
  log "ERROR: Current branch is '$CURRENT_BRANCH' but DEPLOY_BRANCH is '$DEPLOY_BRANCH'."
  log "       Checkout $DEPLOY_BRANCH first, or set REQUIRE_MAIN=0 to override."
  exit 1
fi

DIRTY="$(git status --porcelain 2>/dev/null | wc -l | tr -d ' ')"
if [[ "${DIRTY:-0}" != "0" && "$ALLOW_DIRTY" != "1" ]]; then
  log "ERROR: Uncommitted changes. Commit/stash or set ALLOW_DIRTY=1"
  git status -s
  exit 1
fi

log "=== Deploy start (branch=$DEPLOY_BRANCH, host=$DEPLOY_USER@$DEPLOY_HOST) ==="
pause 1

log "Step 1/3 — git push → origin $DEPLOY_BRANCH"
run git push origin "$DEPLOY_BRANCH"
pause "$DEPLOY_SLEEP"

log "Step 2/3 — SSH to production ($DEPLOY_USER@$DEPLOY_HOST)"
pause 1

if [[ "$DRY_RUN" == "1" ]]; then
  echo "[dry-run] ssh ${SSH_OPTS[*]} $DEPLOY_USER@$DEPLOY_HOST '(remote: git pull + docker compose)'"
else
  # shellcheck disable=SC2029
  ssh "${SSH_OPTS[@]}" "$DEPLOY_USER@$DEPLOY_HOST" bash <<EOF
set -euo pipefail
cd $(printf %q "$DEPLOY_PATH")
echo "[remote \$(date -u +%Y-%m-%dT%H:%M:%SZ)] cwd: \$(pwd)"
git fetch origin
git pull --ff-only origin $(printf %q "$DEPLOY_BRANCH")
docker compose -f docker-compose.prod.yml --env-file .env.production up -d --build
echo "[remote \$(date -u +%Y-%m-%dT%H:%M:%SZ)] done"
EOF
fi

pause "$DEPLOY_SLEEP"
log "=== Deploy complete ==="
