"""Shared pagination helpers for list views/templates."""

PAGE_SIZE_OPTIONS = (100, 300, 500, 700)


def get_page_size(request, default=100, options=None):
    raw = (request.GET.get("page_size") or "").strip()
    choices = tuple(options or PAGE_SIZE_OPTIONS)
    if raw.isdigit():
        value = int(raw)
        if value in choices:
            return value
    return default


def build_pagination_window(page_obj, radius=2):
    """
    Build compact pagination numbers with ellipses.
    Example output: [1, None, 8, 9, 10, 11, 12, None, 40]
    """
    total = page_obj.paginator.num_pages
    current = page_obj.number
    if total <= (radius * 2 + 5):
        return list(range(1, total + 1))

    pages = {1, total}
    start = max(1, current - radius)
    end = min(total, current + radius)
    pages.update(range(start, end + 1))
    ordered = sorted(pages)

    out = []
    prev = None
    for p in ordered:
        if prev is not None and p - prev > 1:
            out.append(None)
        out.append(p)
        prev = p
    return out
