"""
Local Harvesting Agent — minimal Django settings.

Used when running the harvesting agent on a local machine or cheap VPS.
Extends the production settings but strips out web-server-only components
(Tailwind, browser-reload, HTMX, Whitenoise) that are not needed for harvesting.

Usage:
  DJANGO_SETTINGS_MODULE=config.settings_local_harvester python manage.py harvest_and_push ...

Or set in .env.harvester:
  DJANGO_SETTINGS_MODULE=config.settings_local_harvester
"""

from pathlib import Path
from decouple import config
import dj_database_url
import os
import sys

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR / "apps"))

# ── Core ──────────────────────────────────────────────────────────────────────

SECRET_KEY = config("SECRET_KEY", default="local-harvester-insecure-key-not-for-web")
DEBUG = False
ALLOWED_HOSTS = ["*"]

# ── Database ──────────────────────────────────────────────────────────────────
# direct mode → set DATABASE_URL to prod PostgreSQL connection string
# push mode   → set DATABASE_URL to a local SQLite or any DB (only used for Company/Label lookups)

DATABASES = {
    "default": dj_database_url.config(
        default=config("DATABASE_URL", default="sqlite:///local_harvest.db"),
        conn_max_age=600,
    )
}

# ── Apps — only what the harvest engine needs ─────────────────────────────────

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    # Celery results (needed even when ALWAYS_EAGER=True)
    "django_celery_beat",
    "django_celery_results",
    # Core platform apps needed by harvest
    "users.apps.UsersConfig",
    "core.apps.CoreConfig",
    "jobs.apps.JobsConfig",
    "companies",
    "harvest.apps.HarvestConfig",
    # Stubs — included so migrations don't break, but not actively used
    "resumes.apps.ResumesConfig",
    "submissions.apps.SubmissionsConfig",
    "messaging.apps.MessagingConfig",
    "analytics.apps.AnalyticsConfig",
    "interviews_app.apps.InterviewsAppConfig",
    "prompts_app.apps.PromptsAppConfig",
]

AUTH_USER_MODEL = "users.User"

# ── Minimal middleware (no web features) ──────────────────────────────────────

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
]

ROOT_URLCONF = "config.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    }
]

WSGI_APPLICATION = "config.wsgi.application"

# ── Celery — run tasks synchronously in the same process (no broker needed) ──

CELERY_TASK_ALWAYS_EAGER = True        # Tasks execute immediately, in-process
CELERY_TASK_EAGER_PROPAGATES = True    # Exceptions surface immediately
CELERY_BROKER_URL = config("CELERY_BROKER_URL", default="memory://")
CELERY_RESULT_BACKEND = config("CELERY_RESULT_BACKEND", default="cache+memory://")
CELERY_ACCEPT_CONTENT = ["json"]
CELERY_TASK_SERIALIZER = "json"
CELERY_RESULT_SERIALIZER = "json"
CELERY_TASK_DEFAULT_QUEUE = "default"

# ── Harvest engine tuning for local agent ─────────────────────────────────────

# Increase Jarvis concurrency — local machine has no hosting constraints
JARVIS_HTTP_MAX_GLOBAL = config("JARVIS_HTTP_MAX_GLOBAL", default=500, cast=int)
JARVIS_HTTP_MAX_PER_HOST = config("JARVIS_HTTP_MAX_PER_HOST", default=20, cast=int)
JARVIS_HTTP_RETRY_MAX = config("JARVIS_HTTP_RETRY_MAX", default=3, cast=int)
JARVIS_HTTP_RETRY_BASE_SEC = config("JARVIS_HTTP_RETRY_BASE_SEC", default=0.5, cast=float)

HARVEST_BACKFILL_INTER_JOB_DELAY_SEC = config(
    "HARVEST_BACKFILL_INTER_JOB_DELAY_SEC", default=0.02, cast=float  # faster than prod default
)
HARVEST_JD_STALE_DAYS = config("HARVEST_JD_STALE_DAYS", default=120, cast=int)

# Local Harvesting Agent auth token (must match prod HARVEST_PUSH_SECRET)
HARVEST_PUSH_SECRET = config("HARVEST_PUSH_SECRET", default="").strip()

# ── Localisation ──────────────────────────────────────────────────────────────

LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True

# ── Static/media (required by Django even if unused) ─────────────────────────

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STATICFILES_DIRS = []

MEDIA_URL = "media/"
MEDIA_ROOT = BASE_DIR / "media"

# ── Cache (in-memory, no Redis needed) ───────────────────────────────────────

CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
        "LOCATION": "harvester-locmem",
    }
}

# ── Optional: dummy values for apps that need them ───────────────────────────

LLM_ENCRYPTION_KEY = config("LLM_ENCRYPTION_KEY", default="")
GOOGLE_KG_API_KEY = config("GOOGLE_KG_API_KEY", default="")
SENTRY_DSN = ""

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# ── Logging ───────────────────────────────────────────────────────────────────

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "simple": {"format": "%(asctime)s [%(levelname)s] %(name)s: %(message)s"},
    },
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "formatter": "simple",
        },
    },
    "root": {"handlers": ["console"], "level": "WARNING"},
    "loggers": {
        "harvest": {"handlers": ["console"], "level": "INFO", "propagate": False},
    },
}
