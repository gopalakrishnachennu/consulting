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
    SITE_TAGLINE as DEFAULT_SITE_TAGLINE,
    SITE_DESCRIPTION as DEFAULT_SITE_DESCRIPTION,
    SITE_FULL_TITLE as DEFAULT_SITE_FULL_TITLE,
    COMPANY_NAME as DEFAULT_COMPANY_NAME,
    COMPANY_EMAIL as DEFAULT_COMPANY_EMAIL,
    COMPANY_PHONE as DEFAULT_COMPANY_PHONE,
    COPYRIGHT_TEXT,
    META_DESCRIPTION as DEFAULT_META_DESCRIPTION,
    META_KEYWORDS as DEFAULT_META_KEYWORDS,
    SOCIAL_TWITTER as DEFAULT_SOCIAL_TWITTER,
    SOCIAL_LINKEDIN as DEFAULT_SOCIAL_LINKEDIN,
    SOCIAL_GITHUB as DEFAULT_SOCIAL_GITHUB,
)
from config.constants.messages import MSG_HOME_CTA


def site_config(request):
    """Inject site-wide branding and config into all templates."""
    from core.services import PlatformConfigService

    config = PlatformConfigService.get_config()
    site_name = (config.site_name if config and getattr(config, 'site_name', None) else None) or DEFAULT_SITE_NAME
    site_tagline = (getattr(config, 'site_tagline', '') or '').strip() or DEFAULT_SITE_TAGLINE
    meta_description = (getattr(config, 'meta_description', '') or '').strip() or site_tagline or DEFAULT_META_DESCRIPTION
    meta_keywords = (getattr(config, 'meta_keywords', '') or '').strip() or DEFAULT_META_KEYWORDS
    company_email = (getattr(config, 'contact_email', '') or '').strip() or DEFAULT_COMPANY_EMAIL
    company_phone = (getattr(config, 'support_phone', '') or '').strip() or DEFAULT_COMPANY_PHONE
    site_description = meta_description or DEFAULT_SITE_DESCRIPTION
    site_full_title = f"{site_name} | {site_tagline}" if site_tagline else DEFAULT_SITE_FULL_TITLE
    company_name = site_name or DEFAULT_COMPANY_NAME

    return {
        # Branding (site name from platform config so Settings → Branding is used everywhere)
        'SITE_NAME': site_name,
        'SITE_TAGLINE': site_tagline,
        'SITE_DESCRIPTION': site_description,
        'SITE_FULL_TITLE': site_full_title,
        'COMPANY_NAME': company_name,
        'COMPANY_EMAIL': company_email,
        'COMPANY_PHONE': company_phone,
        'COPYRIGHT_TEXT': COPYRIGHT_TEXT,
        'META_DESCRIPTION': meta_description,
        'META_KEYWORDS': meta_keywords,

        # Social
        'SOCIAL_TWITTER': (getattr(config, 'twitter_url', '') or '').strip() or DEFAULT_SOCIAL_TWITTER,
        'SOCIAL_LINKEDIN': (getattr(config, 'linkedin_url', '') or '').strip() or DEFAULT_SOCIAL_LINKEDIN,
        'SOCIAL_GITHUB': (getattr(config, 'github_url', '') or '').strip() or DEFAULT_SOCIAL_GITHUB,

        # Messages (built from platform config site name)
        'MSG_LOGIN_HEADING': f"Login to {site_name}",
        'MSG_HOME_WELCOME': f"Welcome to {site_name}",
        'MSG_HOME_CTA': MSG_HOME_CTA,

        # Impersonate
        'is_impersonating': getattr(request, 'is_impersonating', False),
        'real_user': getattr(request, 'real_user', None),
    }
