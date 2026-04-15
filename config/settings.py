from pathlib import Path
# Trigger reload
from decouple import config
import dj_database_url
import os
import sys

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR / 'apps'))

SECRET_KEY = config('SECRET_KEY', default='django-insecure-change-me-in-production')
DEBUG = config('DEBUG', default=False, cast=bool)


def _csv_list(value: str) -> list[str]:
    return [x.strip() for x in (value or '').split(',') if x.strip()]


# Production domain (Namecheap). Override ALLOWED_HOSTS / CSRF_TRUSTED_ORIGINS / SITE_URL in .env for staging.
_PUBLIC_DOMAIN = 'chennu.co'
_PUBLIC_DOMAIN_WWW = 'www.chennu.co'

ALLOWED_HOSTS = _csv_list(
    config(
        'ALLOWED_HOSTS',
        default=f'127.0.0.1,localhost,{_PUBLIC_DOMAIN},{_PUBLIC_DOMAIN_WWW}',
    )
)

# Django 4+: required for HTTPS POSTs / admin when Origin differs; includes local dev ports.
_CSRF_DEFAULT = (
    f'https://{_PUBLIC_DOMAIN},https://{_PUBLIC_DOMAIN_WWW},'
    'http://127.0.0.1:8000,http://localhost:8000'
)
CSRF_TRUSTED_ORIGINS = _csv_list(config('CSRF_TRUSTED_ORIGINS', default=_CSRF_DEFAULT))

LLM_ENCRYPTION_KEY = config('LLM_ENCRYPTION_KEY', default='')

INSTALLED_APPS = [
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
    
    # Third-party
    'tailwind',
    'theme',
    'django_browser_reload',
    'django_htmx',
    'django_extensions',
    'widget_tweaks',
    'django_celery_beat',
    'django_celery_results',
    
    # Local Apps
    'users.apps.UsersConfig',
    'core.apps.CoreConfig',
    'jobs.apps.JobsConfig',
    'resumes.apps.ResumesConfig',
    'submissions.apps.SubmissionsConfig',
    'messaging.apps.MessagingConfig',
    'analytics.apps.AnalyticsConfig',
    'companies',
    'harvest.apps.HarvestConfig',
    'interviews_app.apps.InterviewsAppConfig',
    'prompts_app.apps.PromptsAppConfig',  # kept for migration chain — UI/URLs fully removed
]

AUTH_USER_MODEL = 'users.User'

MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    'whitenoise.middleware.WhiteNoiseMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django_htmx.middleware.HtmxMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'core.middleware.AuditMiddleware',  # Audit Log
    'config.middleware.ImpersonateMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
    'config.middleware.RequestLoggingMiddleware',
    'django_browser_reload.middleware.BrowserReloadMiddleware',
]

ROOT_URLCONF = 'config.urls'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [BASE_DIR / 'templates'],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
                'core.context_processors.platform_settings',  # Added Platform Config
                'config.context_processors.site_config',
                'messaging.context_processors.unread_messages_count',
                'core.context_processors.unread_notifications_count',
                'core.context_processors.pending_pool_count',  # Job pool badge in nav
                'core.context_processors.user_feature_flags',
            ],
        },
    },
]

WSGI_APPLICATION = 'config.wsgi.application'

DATABASES = {
    'default': dj_database_url.config(
        default=config('DATABASE_URL', default='sqlite:///db.sqlite3'),
        conn_max_age=600
    )
}

AUTH_PASSWORD_VALIDATORS = [
    {
        'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator',
    },
]

LANGUAGE_CODE = 'en-us'
TIME_ZONE = 'UTC'
USE_I18N = True
USE_TZ = True

# Consultant workflow dashboard: flag sidebar rows when assigned/drafting work is older than N days
WORKFLOW_STALE_DAYS = int(config('WORKFLOW_STALE_DAYS', default='7'))

# Job CSV bulk upload: max uploaded file size (MB). Replaces an old check that rejected files > 64KB.
JOB_BULK_UPLOAD_MAX_MB = max(1, int(config('JOB_BULK_UPLOAD_MAX_MB', default='50')))
# Multipart job CSVs must fit in memory for DictReader path; align with bulk limit unless overridden.
_bulk_upload_bytes = JOB_BULK_UPLOAD_MAX_MB * 1024 * 1024
DATA_UPLOAD_MAX_MEMORY_SIZE = int(
    config('DATA_UPLOAD_MAX_MEMORY_SIZE', default=str(_bulk_upload_bytes))
)
FILE_UPLOAD_MAX_MEMORY_SIZE = int(
    config('FILE_UPLOAD_MAX_MEMORY_SIZE', default=str(_bulk_upload_bytes))
)

# Optional: Google Knowledge Graph Search API key for company enrichment (used when Platform Config field is empty)
GOOGLE_KG_API_KEY = config('GOOGLE_KG_API_KEY', default='').strip()

# Static files
STATIC_URL = 'static/'
STATIC_ROOT = BASE_DIR / 'staticfiles'
STATICFILES_DIRS = [BASE_DIR / 'static']
STATICFILES_STORAGE = 'whitenoise.storage.CompressedManifestStaticFilesStorage'

# Media files
MEDIA_URL = 'media/'
MEDIA_ROOT = BASE_DIR / 'media'

# Tailwind
TAILWIND_APP_NAME = 'theme'
INTERNAL_IPS = ["127.0.0.1"]
NPM_BIN_PATH = "npm"

# ── Celery ────────────────────────────────────────────────────────────────────
CELERY_BROKER_URL = config('CELERY_BROKER_URL', default='redis://localhost:6379/0')
# Store task results in DB (django-celery-results) so they survive restarts.
# Override via env: CELERY_RESULT_BACKEND=django-db (Docker) or redis://... (local)
CELERY_RESULT_BACKEND = config('CELERY_RESULT_BACKEND', default='django-db')
CELERY_TASK_ALWAYS_EAGER = config('CELERY_TASK_ALWAYS_EAGER', default=False, cast=bool)
CELERY_CACHE_BACKEND = 'django-cache'
CELERY_RESULT_EXTENDED = True
CELERY_TASK_TRACK_STARTED = True
CELERY_TASK_SEND_SENT_EVENT = True

# Serialization
CELERY_ACCEPT_CONTENT = ['json']
CELERY_TASK_SERIALIZER = 'json'
CELERY_RESULT_SERIALIZER = 'json'

# Task time limits
CELERY_TASK_SOFT_TIME_LIMIT = 300   # 5 min soft limit
CELERY_TASK_TIME_LIMIT = 600        # 10 min hard kill

# Concurrency (set via env in production)
CELERY_WORKER_CONCURRENCY = config('CELERY_WORKER_CONCURRENCY', default=2, cast=int)
CELERY_WORKER_MAX_TASKS_PER_CHILD = 100  # restart worker after 100 tasks to prevent memory leaks
CELERY_WORKER_PREFETCH_MULTIPLIER = 1    # fair task distribution

# Default primary key field type
DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'

# Used by messaging typing indicators, rate limits, etc. Use Redis in multi-instance production.
CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
        "LOCATION": "app-locmem",
    }
}

LOGIN_REDIRECT_URL = '/'
LOGOUT_REDIRECT_URL = '/'

# Public site base URL for absolute links in emails (no trailing slash). Local: set SITE_URL=http://127.0.0.1:8000 in .env
SITE_URL = config('SITE_URL', default=f'https://{_PUBLIC_DOMAIN}').rstrip('/')

# Email — defaults suit local/dev (console). In production set EMAIL_* and typically SMTP.
EMAIL_BACKEND = config(
    'EMAIL_BACKEND',
    default='django.core.mail.backends.console.EmailBackend',
)
EMAIL_HOST = config('EMAIL_HOST', default='')
EMAIL_PORT = config('EMAIL_PORT', default=587, cast=int)
EMAIL_USE_TLS = config('EMAIL_USE_TLS', default=True, cast=bool)
EMAIL_HOST_USER = config('EMAIL_HOST_USER', default='')
EMAIL_HOST_PASSWORD = config('EMAIL_HOST_PASSWORD', default='')
DEFAULT_FROM_EMAIL = config('DEFAULT_FROM_EMAIL', default='noreply@localhost')

# --- Error monitoring (Sentry) — set SENTRY_DSN in production ---
SENTRY_DSN = config('SENTRY_DSN', default='').strip()
if SENTRY_DSN:
    import sentry_sdk
    from sentry_sdk.integrations.django import DjangoIntegration

    sentry_sdk.init(
        dsn=SENTRY_DSN,
        integrations=[DjangoIntegration()],
        send_default_pii=False,
        environment=config('SENTRY_ENVIRONMENT', default='development' if DEBUG else 'production'),
        traces_sample_rate=config('SENTRY_TRACES_SAMPLE_RATE', default=0.0, cast=float),
    )

# --- Advanced Logging ---
from config.logging_config import get_logging_config
LOGGING = get_logging_config(debug=DEBUG)
