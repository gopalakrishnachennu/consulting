from django import template

from core.feature_flags import feature_enabled_for

register = template.Library()


@register.simple_tag(takes_context=True)
def feature_enabled(context, key: str) -> bool:
    request = context.get('request')
    if not request or not getattr(request, 'user', None).is_authenticated:
        return False
    return feature_enabled_for(request.user, key)


@register.filter
def get_item(mapping, key):
    if mapping is None:
        return set()
    return mapping.get(key, set())
