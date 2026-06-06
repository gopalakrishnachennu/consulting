"""
Phase 3e: Header — Deterministic header assembly.

No LLM calls. Uses resolved location from engine.py logic.
"""
from resumes.engine import get_resume_location


def generate_header(consultant, job=None, use_preferred_location=False):
    """
    Generate the resume header block: Name + contact info line.

    Returns plain text:
        John Doe
        Spokane, WA | john@email.com | 555-123-4567
    """
    user = consultant.user
    name = user.get_full_name() or user.username

    # Resolve location using engine.py's existing logic
    location, _source = get_resume_location(
        consultant, job, use_preferred=use_preferred_location
    )

    contact_parts = []
    if location:
        contact_parts.append(location)
    if user.email:
        contact_parts.append(user.email)
    if consultant.phone:
        contact_parts.append(consultant.phone)

    contact_line = " | ".join(contact_parts)
    return f"{name}\n{contact_line}".strip()
