"""HTTP helpers (redirects with query params)."""

from __future__ import annotations

from urllib.parse import urlencode

from django.shortcuts import redirect
from django.urls import reverse


def redirect_with_task_progress(
    viewname: str,
    task_id: str,
    label: str,
    *,
    args: tuple | None = None,
    kwargs: dict | None = None,
):
    """
    Redirect to a named URL with ``?tp=<celery task id>&tpl=<label>`` for the global progress bar.

    ``label`` is shown in the UI (keep short).
    """
    path = reverse(viewname, args=args or [], kwargs=kwargs or {})
    q = urlencode({"tp": task_id, "tpl": label})
    return redirect(f"{path}?{q}")
