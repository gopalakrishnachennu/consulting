"""
Context Processor: Injects branding & config constants into every template.
Branding (site name, login heading, etc.) comes from PlatformConfig when set;
otherwise falls back to config.constants.branding.

Usage in templates:
    {{ SITE_NAME }}
    {{ PLATFORM_CONFIG.site_name }}
    {{ MSG_LOGIN_HEADING }}
    etc.
"""

from config.constants.branding import (
    SITE_NAME as DEFAULT_SITE_NAME,
    SITE_TAGLINE, SITE_DESCRIPTION, SITE_FULL_TITLE,
    COMPANY_NAME, COMPANY_EMAIL, COMPANY_PHONE,
    COPYRIGHT_TEXT, META_DESCRIPTION, META_KEYWORDS,
    SOCIAL_TWITTER, SOCIAL_LINKEDIN, SOCIAL_GITHUB,
)
from config.constants.messages import MSG_HOME_CTA


def site_config(request):
    """Inject site-wide branding and config into all templates."""
    from core.services import PlatformConfigService

    config = PlatformConfigService.get_config()
    site_name = (config.site_name if config and getattr(config, 'site_name', None) else None) or DEFAULT_SITE_NAME

    return {
        # Branding (site name from platform config so Settings → Branding is used everywhere)
        'SITE_NAME': site_name,
        'SITE_TAGLINE': SITE_TAGLINE,
        'SITE_DESCRIPTION': SITE_DESCRIPTION,
        'SITE_FULL_TITLE': SITE_FULL_TITLE,
        'COMPANY_NAME': COMPANY_NAME,
        'COMPANY_EMAIL': COMPANY_EMAIL,
        'COMPANY_PHONE': COMPANY_PHONE,
        'COPYRIGHT_TEXT': COPYRIGHT_TEXT,
        'META_DESCRIPTION': META_DESCRIPTION,
        'META_KEYWORDS': META_KEYWORDS,

        # Social
        'SOCIAL_TWITTER': SOCIAL_TWITTER,
        'SOCIAL_LINKEDIN': SOCIAL_LINKEDIN,
        'SOCIAL_GITHUB': SOCIAL_GITHUB,

        # Messages (built from platform config site name)
        'MSG_LOGIN_HEADING': f"Login to {site_name}",
        'MSG_HOME_WELCOME': f"Welcome to {site_name}",
        'MSG_HOME_CTA': MSG_HOME_CTA,

        # Impersonate
        'is_impersonating': getattr(request, 'is_impersonating', False),
        'real_user': getattr(request, 'real_user', None),
    }
