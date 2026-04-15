"""
Celery task progress for staff UI polling.

Uses the Celery result backend (django-db) via ``update_state(state='PROGRESS', meta=…)``.
Tasks must use ``@shared_task(bind=True)`` and call ``update_task_progress`` in loops.
"""

from __future__ import annotations


def update_task_progress(
    task_self,
    *,
    current: int,
    total: int,
    message: str = "",
) -> None:
    """Publish PROGRESS for :class:`celery.result.AsyncResult` / task progress API."""
    total_i = max(int(total), 0)
    current_i = max(int(current), 0)
    if total_i:
        current_i = min(current_i, total_i)
        pct = int(100 * current_i / total_i)
    else:
        pct = 100 if current_i else 0
    task_self.update_state(
        state="PROGRESS",
        meta={
            "current": current_i,
            "total": total_i,
            "percent": pct,
            "message": (message or "")[:500],
        },
    )
