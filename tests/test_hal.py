"""HAL unwrapping and auto-pagination."""

from __future__ import annotations

from typing import Any

import pytest

from jsonhub_cli import hal, paging

from .conftest import hal_collection


def test_items_reads_the_standard_embedded_key() -> None:
    body = hal_collection({"id": "a"}, {"id": "b"})
    assert [item["id"] for item in hal.items(body)] == ["a", "b"]


def test_items_falls_back_to_a_differently_named_embedded_list() -> None:
    # _embedded has free-form additionalProperties in the spec, so a deployment
    # may name the list after the resource.
    body = {"_links": {}, "_embedded": {"entities": [{"id": "a"}]}}
    assert [item["id"] for item in hal.items(body)] == ["a"]


def test_items_is_empty_when_embedded_is_absent() -> None:
    assert hal.items(hal_collection()) == []


def test_items_ignores_non_object_members() -> None:
    body = {"_links": {}, "_embedded": {"item": [{"id": "a"}, "junk", None]}}
    assert [item["id"] for item in hal.items(body)] == ["a"]


def test_page_reports_pagination_metadata() -> None:
    result = hal.page(hal_collection({"id": "a"}, total=42, next_page=True))
    assert (result.total_items, result.has_next, len(result)) == (42, True, 1)


def test_collect_stops_at_the_requested_limit() -> None:
    requested: list[tuple[int, int]] = []

    def fetch(page: int, size: int) -> dict[str, Any]:
        requested.append((page, size))
        return hal_collection(*({"id": f"{page}-{n}"} for n in range(size)), total=500, next_page=True)

    result = paging.collect(fetch, limit=250)

    assert len(result.items) == 250
    # The page size stays at the cap and the overshoot is trimmed; shrinking it
    # for the last request would re-offset the pages and duplicate rows.
    assert requested == [(1, 100), (2, 100), (3, 100)]


def test_collect_never_returns_duplicates_when_paginating() -> None:
    """A page number means "this offset at this page size", so the size must not
    change mid-traversal."""
    pages = {
        1: [{"id": f"item-{n}"} for n in range(1, 101)],
        2: [{"id": f"item-{n}"} for n in range(101, 201)],
    }

    def fetch(page: int, size: int) -> dict[str, Any]:
        assert size == 100, f"page size changed to {size} mid-traversal"
        return hal_collection(*pages[page], total=200, next_page=page < 2)

    result = paging.collect(fetch, limit=120)

    ids = [item["id"] for item in result.items]
    assert len(ids) == 120
    assert len(set(ids)) == 120


def test_collect_asks_for_a_small_page_when_the_limit_is_small() -> None:
    requested: list[tuple[int, int]] = []

    def fetch(page: int, size: int) -> dict[str, Any]:
        requested.append((page, size))
        return hal_collection(*({"id": str(n)} for n in range(size)), total=500, next_page=True)

    paging.collect(fetch, limit=5)

    assert requested == [(1, 5)]


def test_collect_stops_when_a_page_comes_back_short() -> None:
    def fetch(page: int, size: int) -> dict[str, Any]:
        return hal_collection({"id": "only"}, total=1)

    result = paging.collect(fetch, limit=100)
    assert len(result.items) == 1


def test_collect_stops_on_an_empty_page_despite_a_next_link() -> None:
    # Asking for a page past the end returns no members but still advertises
    # _links.next, which would otherwise loop forever.
    calls = 0

    def fetch(page: int, size: int) -> dict[str, Any]:
        nonlocal calls
        calls += 1
        return hal_collection(total=138, next_page=True)

    result = paging.collect(fetch, limit=100)
    assert (calls, result.items) == (1, [])


def test_explicit_page_makes_exactly_one_request() -> None:
    requested: list[tuple[int, int]] = []

    def fetch(page: int, size: int) -> dict[str, Any]:
        requested.append((page, size))
        return hal_collection({"id": "x"}, total=500, next_page=True)

    paging.collect(fetch, limit=25, page=7)
    assert requested == [(7, 25)]


def test_collect_rejects_a_nonsense_limit() -> None:
    with pytest.raises(ValueError, match="limit must be positive"):
        paging.collect(lambda page, size: hal_collection(), limit=0)
