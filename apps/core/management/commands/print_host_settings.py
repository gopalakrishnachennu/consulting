"""Print Django host / proxy settings (debug HTTP 400 DisallowedHost in production)."""

from django.conf import settings
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Print ALLOWED_HOSTS, proxy trust flags, DEBUG (run inside the same env as the web container)."

    def handle(self, *args, **options):
        self.stdout.write(f"DEBUG={settings.DEBUG}")
        self.stdout.write(f"ALLOWED_HOSTS={settings.ALLOWED_HOSTS}")
        self.stdout.write(f"USE_X_FORWARDED_HOST={getattr(settings, 'USE_X_FORWARDED_HOST', False)}")
        self.stdout.write(
            "SECURE_PROXY_SSL_HEADER="
            + repr(getattr(settings, "SECURE_PROXY_SSL_HEADER", None))
        )
        self.stdout.write(f"SITE_URL={getattr(settings, 'SITE_URL', '')}")
        self.stdout.write(
            "\nIf you still see HTTP 400 on every page, check server logs for "
            "'DisallowedHost' and add that hostname to ALLOWED_HOSTS or "
            "ADDITIONAL_ALLOWED_HOSTS, or ensure your proxy sends X-Forwarded-Host."
        )
