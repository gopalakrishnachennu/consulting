from django import template

register = template.Library()


@register.filter
def can_edit_message(message, user):
    return message.can_edit(user)


@register.filter
def can_remove_message(message, user):
    return message.can_be_removed_by(user)
