"""Unwrapping HAL+JSON collections.

JsonHub returns collections as::

    {
      "_links": {"self": {...}, "first": {...}, "next": {...}, "last": {...}},
      "totalItems": 138,
      "itemsPerPage": 10,
      "_embedded": {"item": [ {...}, {...} ]}
    }

The embedded key is nominally ``item`` but the OpenAPI schema declares
``_embedded`` with free-form ``additionalProperties``, so a deployment may name
it after the resource instead. :func:`items` therefore falls back to "the first
embedded value that is a list of objects" rather than hardcoding ``item``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

EMBEDDED_KEY = "item"


@dataclass(frozen=True)
class Page:
    """One page of a HAL collection, plus enough metadata to keep paging."""

    items: list[dict[str, Any]]
    total_items: int | None
    items_per_page: int | None
    has_next: bool

    def __len__(self) -> int:
        return len(self.items)


def items(collection: dict[str, Any]) -> list[dict[str, Any]]:
    """Extract the embedded resources from a HAL collection body."""
    embedded = collection.get("_embedded")
    if not isinstance(embedded, dict):
        return []
    candidates = [embedded.get(EMBEDDED_KEY), *embedded.values()]
    for value in candidates:
        if isinstance(value, list):
            return [entry for entry in value if isinstance(entry, dict)]
    return []


def page(collection: dict[str, Any]) -> Page:
    """Read one HAL collection body into a :class:`Page`."""
    links = collection.get("_links")
    has_next = isinstance(links, dict) and "next" in links
    return Page(
        items=items(collection),
        total_items=_as_int(collection.get("totalItems")),
        items_per_page=_as_int(collection.get("itemsPerPage")),
        has_next=has_next,
    )


def _as_int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None
