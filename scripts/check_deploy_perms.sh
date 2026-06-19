#!/usr/bin/env bash
# Fail-fast: can THIS gh token dispatch the production deploy on the consulting repo?
# Run this BEFORE any release so permission/path problems surface immediately,
# instead of after a push when deploy/path confusion has already started.
set -euo pipefail

WORKFLOW="deploy-vps.yml"
REPO="${DEPLOY_REPO:-$(gh repo view --json nameWithOwner -q .nameWithOwner 2>/dev/null || true)}"

red() { printf '\033[31m%s\033[0m\n' "$*"; }
grn() { printf '\033[32m%s\033[0m\n' "$*"; }

# 1. authenticated?
if ! gh auth status >/dev/null 2>&1; then
  red "❌ gh is not authenticated. Run: gh auth login"
  exit 1
fi

# 2. repo resolved?
if [ -z "${REPO:-}" ]; then
  red "❌ Could not detect repo. Run inside the repo, or set DEPLOY_REPO=owner/name."
  exit 1
fi

# 3. write/push access — required for workflow_dispatch
PUSH=$(gh api "repos/$REPO" --jq '.permissions.push // false' 2>/dev/null || echo "false")
if [ "$PUSH" != "true" ]; then
  red "❌ This token CANNOT dispatch the deploy on $REPO (no push/write access)."
  red "   Fix: use a credential with repo write/admin, then:"
  red "   gh workflow run $WORKFLOW --repo $REPO --ref main -f confirm=DEPLOY"
  exit 2
fi

# 4. the workflow exists and is active
if ! gh workflow view "$WORKFLOW" --repo "$REPO" >/dev/null 2>&1; then
  red "❌ Workflow '$WORKFLOW' not found / not active on $REPO."
  exit 3
fi

grn "✓ Deploy permission OK — $REPO can dispatch '$WORKFLOW'."
