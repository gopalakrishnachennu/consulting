#!/usr/bin/env sh
# ─────────────────────────────────────────────────────────────────────────────
# GoCareers — Celery Worker
# Picks up tasks from Redis queue and executes them.
# Runs 24/7 — completely independent of the web server.
# Closing the browser does NOT affect this process.
# ─────────────────────────────────────────────────────────────────────────────
set -eu

export PYTHONPATH="${PYTHONPATH:-/app/apps}"
export DJANGO_SETTINGS_MODULE="${DJANGO_SETTINGS_MODULE:-config.settings}"

CONCURRENCY="${CELERY_WORKER_CONCURRENCY:-2}"
LOG_LEVEL="${CELERY_LOG_LEVEL:-info}"

echo "▶ Starting Celery worker (concurrency=$CONCURRENCY, loglevel=$LOG_LEVEL)"

exec celery -A config worker \
  --loglevel="$LOG_LEVEL" \
  --concurrency="$CONCURRENCY" \
  --max-tasks-per-child=100 \
  --queues=celery,high_priority \
  --without-gossip \
  --without-mingle
