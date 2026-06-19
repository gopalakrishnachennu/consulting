#!/usr/bin/env bash
# Canonical, verified production release for chennu.co.
#
#   on main + pushed  ->  migration-drift check  ->  CI + image green on HEAD
#   ->  deploy-permission preflight  ->  dispatch deploy  ->  watch  ->  health check
#
# Fails fast at the first problem so a half-finished deploy never reaches prod.
# Usage:  ./scripts/release.sh        (no args; deploys current main)
set -euo pipefail
cd "$(dirname "$0")/.."

WORKFLOW="deploy-vps.yml"
HEALTH_URL="${HEALTH_URL:-https://chennu.co/core/health/}"
REPO="${DEPLOY_REPO:-$(gh repo view --json nameWithOwner -q .nameWithOwner)}"
PY="$(command -v python3 || command -v python || true)"

red() { printf '\033[31m%s\033[0m\n' "$*"; }
grn() { printf '\033[32m%s\033[0m\n' "$*"; }
ylw() { printf '\033[33m%s\033[0m\n' "$*"; }

echo "── Release preflight · $REPO ──"

# 1. on main and in sync with origin
BRANCH=$(git rev-parse --abbrev-ref HEAD)
[ "$BRANCH" = "main" ] || { red "❌ Not on main (on '$BRANCH'). Deploys go from main."; exit 1; }
git fetch origin main -q
[ "$(git rev-parse @)" = "$(git rev-parse origin/main)" ] || {
  red "❌ Local main differs from origin/main. Push (or pull) first."; exit 1; }
HEAD_SHA=$(git rev-parse --short HEAD)
grn "✓ On main, in sync with origin ($HEAD_SHA)."

# 2. migration drift (best-effort; needs a local Django env)
if [ -n "$PY" ] && [ -f manage.py ]; then
  if "$PY" manage.py makemigrations --check --dry-run >/dev/null 2>&1; then
    grn "✓ No model/migration drift."
  else
    ylw "⚠ makemigrations --check reports drift OR no local env — verify before continuing."
  fi
fi

# 3. CI + Docker image green on THIS HEAD
for wf in "CI" "Build & publish Docker image"; do
  CONC=$(gh run list --repo "$REPO" --branch main --limit 12 \
    --json name,headSha,conclusion,status \
    --jq "[.[] | select(.name==\"$wf\" and (.headSha|startswith(\"$HEAD_SHA\")))][0] | \"\(.status):\(.conclusion)\"" 2>/dev/null || echo ":")
  case "$CONC" in
    completed:success) grn "✓ $wf green on $HEAD_SHA" ;;
    completed:*)       red "❌ $wf is NOT green ($CONC) on $HEAD_SHA. Aborting."; exit 1 ;;
    *)                 red "❌ $wf not finished/found ($CONC) on $HEAD_SHA. Wait for CI, then retry."; exit 1 ;;
  esac
done

# 4. deploy permission preflight (fail fast)
bash "$(dirname "$0")/check_deploy_perms.sh"

# 5. dispatch + watch
echo "Dispatching deploy (confirm=DEPLOY) …"
gh workflow run "$WORKFLOW" --repo "$REPO" --ref main -f confirm=DEPLOY
sleep 8
RID=$(gh run list --repo "$REPO" --workflow="$WORKFLOW" --limit 1 --json databaseId --jq '.[0].databaseId')
echo "Watching deploy run $RID …"
if ! gh run watch "$RID" --repo "$REPO" --exit-status --interval 20; then
  red "❌ Deploy run $RID FAILED. Inspect: gh run view $RID --repo $REPO --log-failed"
  exit 1
fi
grn "✓ Deploy run $RID succeeded."

# 6. health check
OK=""
if [ -n "$PY" ]; then
  OK=$(curl -s "$HEALTH_URL" | "$PY" -c "import json,sys;print(json.load(sys.stdin).get('overall',''))" 2>/dev/null || echo "")
fi
if [ "$OK" = "ok" ]; then
  grn "🚀 Release complete — $HEAD_SHA deployed, prod healthy (overall: ok)."
else
  red "⚠ Deploy succeeded but health check did not return ok (got: '${OK:-?}'). Check $HEALTH_URL"
  exit 1
fi
