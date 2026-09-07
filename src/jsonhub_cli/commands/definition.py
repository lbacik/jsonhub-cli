"""``jsonhub definition`` - manage the JSON Schemas that entities validate against."""

from __future__ import annotations

from typing import Annotated, Any

import typer
from jsonhub_sdk.api.definition import (
    api_definitions_get_collection,
    api_definitions_id_delete,
    api_definitions_id_get,
    api_definitions_id_patch,
    api_definitions_post,
)
from jsonhub_sdk.models import (
    DefinitionDefinitionWrite,
    DefinitionDefinitionWriteJsonMergePatch,
    DefinitionDefinitionWriteJsonMergePatchJsonSchema,
    DefinitionDefinitionWriteJsonSchema,
)
from jsonhub_sdk.types import UNSET

from .. import collection, hal, jsonarg, output, paging, refs
from ..api import Session, check, payload
from ._shared import (
    DataOption,
    FieldOption,
    JsonFlag,
    LimitOption,
    PageOption,
    SearchOption,
    YesFlag,
    confirm,
    session,
)

app = typer.Typer(no_args_is_help=True, help="Work with definitions - the JSON Schemas entities are validated against.")

RESOURCE = "definition"
SCHEMA_PREVIEW_WIDTH = 44
TITLE_WIDTH = 32
UUID_WIDTH = 36

ParentEntityOption = Annotated[
    str | None,
    typer.Option("--parent-entity", "-P", help="Parent entity: slug, id or IRI.", metavar="REF"),
]
SlugOption = Annotated[
    str | None,
    typer.Option("--slug", "-s", help="Human-readable identifier for the definition."),
]


@app.command("list")
def list_definitions(
    ctx: typer.Context,
    search: SearchOption = None,
    parent_entity: ParentEntityOption = None,
    owned: Annotated[bool, typer.Option("--owned", help="Only definitions you own.")] = False,
    limit: LimitOption = 30,
    page: PageOption = None,
    as_json: JsonFlag = False,
) -> None:
    """List definitions."""
    sess = session(ctx)
    parent_id = refs.resolve_entity(sess, parent_entity) if parent_entity else None

    def fetch(page_number: int, page_size: int) -> dict[str, Any]:
        return collection.fetch(
            lambda: api_definitions_get_collection.sync_detailed(
                client=sess.client,
                page=page_number,
                limit=page_size,
                qid=search if search else UNSET,
                owned=True if owned else UNSET,
                parent_entity=parent_id if parent_id else UNSET,
            ),
            resource="definition collection",
        )

    result = paging.collect(fetch, limit=limit, page=page)

    if as_json:
        output.print_json(result.items)
        return

    output.print_table(
        [
            output.Col("ID", min_width=UUID_WIDTH),
            output.Col("SLUG", min_width=8),
            output.Col("TITLE", max_width=TITLE_WIDTH),
            output.Col("SCHEMA", max_width=SCHEMA_PREVIEW_WIDTH),
        ],
        (
            [
                refs.resource_id(item),
                item.get("slug"),
                _schema_title(item),
                output.summarize_json(_properties(item), SCHEMA_PREVIEW_WIDTH),
            ]
            for item in result.items
        ),
        empty="No definitions found",
    )
    _print_count(result)


@app.command("get")
def get_definition(
    ctx: typer.Context,
    ref: Annotated[str, typer.Argument(help="Definition slug, id or IRI.", metavar="DEFINITION")],
    schema_only: Annotated[
        bool,
        typer.Option("--schema-only", help="Print only the JSON Schema."),
    ] = False,
    as_json: JsonFlag = False,
) -> None:
    """Show a single definition and its JSON Schema."""
    sess = session(ctx)
    definition = _fetch(sess, ref)

    if schema_only:
        output.print_json(definition.get("jsonSchema") or {})
        return
    if as_json:
        output.print_json(definition)
        return

    output.print_fields(
        [
            ("id", refs.resource_id(definition)),
            ("slug", definition.get("slug")),
            ("title", _schema_title(definition)),
            ("parent entity", refs.relation_label(definition, "parentEntity")),
            ("owned by you", bool(definition.get("isOwnedByCurrentUser"))),
        ]
    )
    output.err.print()
    output.print_json(definition.get("jsonSchema") or {})


@app.command("create")
def create_definition(
    ctx: typer.Context,
    data: DataOption = None,
    field: FieldOption = None,
    slug: SlugOption = None,
    parent_entity: ParentEntityOption = None,
    as_json: JsonFlag = False,
) -> None:
    """Create a definition from a JSON Schema document.

    The schema comes from --data (inline JSON, @file or - for stdin) and/or
    repeated --field pairs.
    """
    sess = session(ctx)
    schema = jsonarg.require_json(data, field, what="JSON Schema")

    body = DefinitionDefinitionWrite(json_schema=DefinitionDefinitionWriteJsonSchema.from_dict(schema))
    if slug:
        body.slug = slug
    if parent_entity:
        body.parent_entity = refs.entity_iri(sess, parent_entity)

    created = payload(api_definitions_post.sync_detailed(client=sess.require_auth(), body=body), resource=RESOURCE)
    _report_saved(created, "Created", as_json=as_json)


@app.command("edit")
def edit_definition(
    ctx: typer.Context,
    ref: Annotated[str, typer.Argument(help="Definition slug, id or IRI.", metavar="DEFINITION")],
    data: DataOption = None,
    field: FieldOption = None,
    slug: SlugOption = None,
    parent_entity: ParentEntityOption = None,
    as_json: JsonFlag = False,
) -> None:
    """Update a definition.

    With no --data, --field or other change, the JSON Schema opens in $EDITOR.
    The schema is replaced wholesale by what you supply.
    """
    sess = session(ctx)
    definition_id = refs.resolve_definition(sess, ref)
    schema = jsonarg.read_json(data, field, what="JSON Schema")

    has_other_change = slug is not None or parent_entity is not None
    if schema is None and not has_other_change:
        current = _fetch(sess, definition_id)
        schema = jsonarg.edit_json(current.get("jsonSchema") or {}, what="JSON Schema")

    body = DefinitionDefinitionWriteJsonMergePatch()
    if schema is not None:
        body.json_schema = DefinitionDefinitionWriteJsonMergePatchJsonSchema.from_dict(schema)
    if slug is not None:
        body.slug = slug
    if parent_entity is not None:
        body.parent_entity = refs.entity_iri(sess, parent_entity)

    updated = payload(
        api_definitions_id_patch.sync_detailed(definition_id, client=sess.require_auth(), body=body),
        resource=RESOURCE,
    )
    _report_saved(updated, "Updated", as_json=as_json)


@app.command("delete")
def delete_definition(
    ctx: typer.Context,
    ref: Annotated[str, typer.Argument(help="Definition slug, id or IRI.", metavar="DEFINITION")],
    yes: YesFlag = False,
) -> None:
    """Delete a definition. This cannot be undone."""
    sess = session(ctx)
    definition_id = refs.resolve_definition(sess, ref)
    confirm(f"Permanently delete definition {definition_id}?", assume_yes=yes)
    check(api_definitions_id_delete.sync_detailed(definition_id, client=sess.require_auth()), resource=RESOURCE)
    output.success(f"Deleted definition {definition_id}")


def _fetch(sess: Session, ref: str) -> dict[str, Any]:
    definition_id = refs.resolve_definition(sess, ref)
    return payload(api_definitions_id_get.sync_detailed(definition_id, client=sess.client), resource=RESOURCE)


def _schema_title(definition: dict[str, Any]) -> str | None:
    schema = definition.get("jsonSchema")
    if not isinstance(schema, dict):
        return None
    title = schema.get("title") or schema.get("$id")
    return str(title) if title else None


def _properties(definition: dict[str, Any]) -> list[str] | None:
    """The schema's top-level property names, as a compact table preview."""
    schema = definition.get("jsonSchema")
    if not isinstance(schema, dict):
        return None
    properties = schema.get("properties")
    return sorted(properties) if isinstance(properties, dict) else None


def _report_saved(definition: dict[str, Any], verb: str, *, as_json: bool) -> None:
    if as_json:
        output.print_json(definition)
        return
    definition_id = refs.resource_id(definition)
    slug = definition.get("slug")
    output.success(f"{verb} definition {definition_id}" + (f" ({slug})" if slug else ""))
    output.print_json(definition.get("jsonSchema") or {})


def _print_count(result: hal.Page) -> None:
    if result.total_items is None:
        return
    shown = len(result.items)
    suffix = f" of {result.total_items}" if result.total_items != shown else ""
    output.note(f"Showing {shown} definition{'' if shown == 1 else 's'}{suffix}")
