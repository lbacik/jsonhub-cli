"""Fetching HAL collections around a defect in the generated SDK.

JsonHub omits ``_embedded`` entirely when a collection has no members::

    {"_links": {"self": {...}}, "totalItems": 0, "itemsPerPage": 10}

The generated collection models declare ``_embedded`` as required and read it
with ``d.pop("_embedded")``, so every empty result -- a search that matches
nothing, a filter with no hits, an account with no tokens -- raises
``KeyError: '_embedded'`` from inside ``sync_detailed``.

There is no hook to fix that before parsing, so the ``KeyError`` is caught here
and read as what it can only mean: an empty collection. ``_links`` is always
present, so a ``KeyError`` naming anything else is a real bug and propagates.

Delete this module once jsonhub-sdk marks ``_embedded`` optional; the call sites
only need ``payload(...)`` back.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from jsonhub_sdk.types import Response

from .api import payload

MISSING_EMBEDDED_KEY = "_embedded"


def _empty() -> dict[str, Any]:
    """A well-formed, zero-member HAL collection body."""
    return {"_links": {}, "_embedded": {"item": []}, "totalItems": 0}


def fetch(call: Callable[[], Response[Any]], *, resource: str) -> dict[str, Any]:
    """Run a generated collection endpoint and return its HAL body as a dict."""
    try:
        response = call()
    except KeyError as exc:
        if exc.args and exc.args[0] == MISSING_EMBEDDED_KEY:
            return _empty()
        raise
    return payload(response, resource=resource)
