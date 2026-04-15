# Procfile — Railway / Render / Heroku deployments
# Each process runs independently. Closing the browser NEVER affects worker or beat.
#
# Railway:  add RAILWAY_RUN_UID=0 env var if permission errors occur
# Render:   set "Start Command" to use the process you want per service

web: sh scripts/entrypoint.sh
worker: PYTHONPATH=/app/apps celery -A config worker --loglevel=info --concurrency=${CELERY_WORKER_CONCURRENCY:-2} --max-tasks-per-child=100 --queues=celery,high_priority
beat: PYTHONPATH=/app/apps celery -A config beat --loglevel=info --scheduler django_celery_beat.schedulers:DatabaseScheduler
