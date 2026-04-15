#!/usr/bin/env sh
# ─────────────────────────────────────────────────────────────────────────────
# GoCareers — Celery Beat Scheduler
# Fires periodic tasks on the defined schedule (see config/celery.py).
# ⚠️  ONLY ONE beat instance should run at a time — duplicate instances will
#     fire duplicate tasks.
# ─────────────────────────────────────────────────────────────────────────────
set -eu

export PYTHONPATH="${PYTHONPATH:-/app/apps}"
export DJANGO_SETTINGS_MODULE="${DJANGO_SETTINGS_MODULE:-config.settings}"

LOG_LEVEL="${CELERY_LOG_LEVEL:-info}"

echo "▶ Starting Celery beat scheduler (loglevel=$LOG_LEVEL)"

exec celery -A config beat \
  --loglevel="$LOG_LEVEL" \
  --scheduler django_celery_beat.schedulers:DatabaseScheduler
