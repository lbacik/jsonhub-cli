"""Auto-pagination for list commands.

``--limit`` counts *resources*, not requests: ``--limit 250`` issues as many
requests as needed and stops at 250 items, the same way ``gh`` behaves. Passing
``--page`` opts out and fetches exactly that one page, which is what you want
when scripting against a stable offset.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from . import hal

MAX_PAGE_SIZE = 100


def collect(
    fetch: Callable[[int, int], dict[str, Any]],
    *,
    limit: int,
    page: int | None = None,
) -> hal.Page:
    """Gather up to ``limit`` items via ``fetch(page, page_size)``.

    ``fetch`` receives 1-based page numbers and returns a raw HAL collection
    body. When ``page`` is given, exactly one request is made with
    ``page_size=limit``.
    """
    if limit <= 0:
        raise ValueError("limit must be positive")

    if page is not None:
        return hal.page(fetch(page, limit))

    # The page size must stay constant for the whole traversal: page numbers are
    # offsets in units of the page size, so shrinking it for the final request
    # would make "page 2" point back inside page 1 and return duplicates. Ask
    # for full pages and trim the overshoot at the end instead.
    page_size = min(MAX_PAGE_SIZE, limit)
    gathered: list[dict[str, Any]] = []
    current = 1
    total_items: int | None = None
    items_per_page: int | None = None
    has_next = False

    while len(gathered) < limit:
        result = hal.page(fetch(current, page_size))
        if total_items is None:
            total_items = result.total_items
        items_per_page = result.items_per_page
        gathered.extend(result.items)
        has_next = result.has_next
        # An empty or short page means the server has nothing more to give,
        # even if _links.next is (incorrectly) still present.
        if not result.items or not result.has_next:
            break
        current += 1

    return hal.Page(
        items=gathered[:limit],
        total_items=total_items,
        items_per_page=items_per_page,
        has_next=has_next or len(gathered) > limit,
    )
