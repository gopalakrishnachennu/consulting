"""Phase 5: Field-level audit logging utilities."""

from .models import AuditLog


def log_field_changes(actor, instance, old_values, new_values, ip_address=None):
    """
    Compare old_values dict to new_values dict and log each changed field.
    """
    model_name = instance.__class__.__name__
    target_id = str(instance.pk)
    changes = []

    for field, old_val in old_values.items():
        new_val = new_values.get(field)
        if str(old_val) != str(new_val):
            changes.append({
                'field': field,
                'old': str(old_val),
                'new': str(new_val),
            })

    if changes:
        AuditLog.objects.create(
            actor=actor,
            action='field_change',
            target_model=model_name,
            target_id=target_id,
            details={'changes': changes},
            ip_address=ip_address,
        )
    return changes
