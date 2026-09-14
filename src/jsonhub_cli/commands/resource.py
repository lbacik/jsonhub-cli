"""Interactive mixed resource listing.

The shell adapter parses its compact ``list`` syntax, while this module owns
the API and presentation policy for resources returned from both collections.
"""

from __future__ import annotations

from typing import Any

from jsonhub_sdk.api.definition import api_definitions_get_collection
from jsonhub_sdk.api.entity import api_entities_get_collection
from jsonhub_sdk.types import UNSET

from .. import collection, output, paging, refs
from ._shared import CliState


def list_current_resources(state: CliState, *, limit: int, as_json: bool) -> None:
    """List entities followed by definitions within one shared limit."""
    entities = _fetch_entities(state, limit)
    remaining = limit - len(entities)
    definitions = _fetch_definitions(state, remaining) if remaining else []
    if as_json:
        output.print_json({"entities": entities, "definitions": definitions})
        return
    output.print_table(
        ["TYPE", output.Col("ID", min_width=36), output.Col("SLUG", min_width=8), "PARENT"],
        (
            [kind, refs.resource_id(item), item.get("slug"), refs.relation_label(item, parent)]
            for kind, parent, items in (
                ("entity", "parent", entities),
                ("definition", "parentEntity", definitions),
            )
            for item in items
        ),
        empty="No resources found",
    )


def _fetch_entities(state: CliState, limit: int) -> list[dict[str, Any]]:
    parent_id = state.current_entity_id
    return paging.collect(
        lambda page, page_size: collection.fetch(
            lambda: api_entities_get_collection.sync_detailed(
                client=state.session.client,
                page=page,
                limit=page_size,
                parent=parent_id if parent_id else UNSET,
            ),
            resource="entity collection",
        ),
        limit=limit,
    ).items


def _fetch_definitions(state: CliState, limit: int) -> list[dict[str, Any]]:
    parent_id = state.current_entity_id
    return paging.collect(
        lambda page, page_size: collection.fetch(
            lambda: api_definitions_get_collection.sync_detailed(
                client=state.session.client,
                page=page,
                limit=page_size,
                parent_entity=parent_id if parent_id else UNSET,
            ),
            resource="definition collection",
        ),
        limit=limit,
    ).items
