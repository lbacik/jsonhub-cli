"""``jsonhub entity`` - create, read, update and delete JSON entities."""

from __future__ import annotations

from typing import Annotated, Any

import typer
from jsonhub_sdk.api.entity import (
    api_entities_get_collection,
    api_entities_id_delete,
    api_entities_id_get,
    api_entities_id_patch,
    api_entities_post,
)
from jsonhub_sdk.models import (
    EntityEntityCreate,
    EntityEntityCreateData,
    EntityEntityUpdateJsonMergePatch,
    EntityEntityUpdateJsonMergePatchData,
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

app = typer.Typer(no_args_is_help=True, help="Work with entities - the JSON documents stored in JsonHub.")

RESOURCE = "entity"
DATA_PREVIEW_WIDTH = 44
UUID_WIDTH = 36

DefinitionOption = Annotated[
    str | None,
    typer.Option("--definition", "-D", help="Definition to validate against: slug, id or IRI.", metavar="REF"),
]
ParentOption = Annotated[
    str | None,
    typer.Option("--parent", "-P", help="Parent entity: slug, id or IRI.", metavar="REF"),
]
SlugOption = Annotated[
    str | None,
    typer.Option("--slug", "-s", help="Human-readable identifier for the entity."),
]
RootOption = Annotated[
    bool | None,
    typer.Option("--root/--nested", help="Only top-level entities (--nested: only entities with a parent)."),
]


@app.command("list")
def list_entities(
    ctx: typer.Context,
    search: SearchOption = None,
    definition: DefinitionOption = None,
    parent: ParentOption = None,
    root: RootOption = None,
    owned: Annotated[bool, typer.Option("--owned", help="Only entities you own.")] = False,
    private: Annotated[bool, typer.Option("--private", help="Only your private entities.")] = False,
    limit: LimitOption = 30,
    page: PageOption = None,
    as_json: JsonFlag = False,
) -> None:
    """List entities."""
    sess = session(ctx)
    definition_id = refs.resolve_definition(sess, definition) if definition else None
    parent_id = refs.resolve_entity(sess, parent) if parent else None

    def fetch(page_number: int, page_size: int) -> dict[str, Any]:
        return collection.fetch(
            lambda: api_entities_get_collection.sync_detailed(
                client=sess.client,
                page=page_number,
                limit=page_size,
                qid=search if search else UNSET,
                owned=True if owned else UNSET,
                private=True if private else UNSET,
                root=UNSET if root is None else root,
                definition=definition_id if definition_id else UNSET,
                parent=parent_id if parent_id else UNSET,
            ),
            resource="entity collection",
        )

    result = paging.collect(fetch, limit=limit, page=page)

    if as_json:
        output.print_json(result.items)
        return

    output.print_table(
        [
            output.Col("ID", min_width=UUID_WIDTH),
            output.Col("SLUG", min_width=8),
            output.Col("DEFINITION", min_width=10),
            output.Col("PRIVATE", min_width=7),
            output.Col("DATA", max_width=DATA_PREVIEW_WIDTH),
        ],
        (
            [
                refs.resource_id(item),
                item.get("slug"),
                refs.relation_label(item, "definition"),
                bool(item.get("private")),
                output.summarize_json(item.get("data"), DATA_PREVIEW_WIDTH),
            ]
            for item in result.items
        ),
        empty="No entities found",
    )
    _print_count(result)


@app.command("get")
def get_entity(
    ctx: typer.Context,
    ref: Annotated[str, typer.Argument(help="Entity slug, id or IRI.", metavar="ENTITY")],
    data_only: Annotated[
        bool,
        typer.Option("--data-only", help="Print only the entity's data document, as JSON."),
    ] = False,
    as_json: JsonFlag = False,
) -> None:
    """Show a single entity."""
    sess = session(ctx)
    entity = _fetch(sess, ref)

    if data_only:
        output.print_json(entity.get("data") or {})
        return
    if as_json:
        output.print_json(entity)
        return

    output.print_fields(
        [
            ("id", refs.resource_id(entity)),
            ("slug", entity.get("slug")),
            ("definition", refs.relation_label(entity, "definition")),
            ("parent", refs.relation_label(entity, "parent")),
            ("private", bool(entity.get("private"))),
            ("owned by you", bool(entity.get("isOwnedByCurrentUser"))),
        ]
    )
    output.err.print()
    output.print_json(entity.get("data") or {})


@app.command("create")
def create_entity(
    ctx: typer.Context,
    data: DataOption = None,
    field: FieldOption = None,
    definition: DefinitionOption = None,
    parent: ParentOption = None,
    slug: SlugOption = None,
    private: Annotated[bool, typer.Option("--private", help="Hide the entity from everyone but you.")] = False,
    as_json: JsonFlag = False,
) -> None:
    """Create an entity from a JSON document.

    The document comes from --data (inline JSON, @file or - for stdin) and/or
    repeated --field pairs.
    """
    sess = session(ctx)
    document = jsonarg.require_json(data, field, what="entity data")

    # definition is required by the generated model -- it serialises as an
    # explicit null, which is how the API reads "no definition to validate
    # against" -- so it has to be resolved before the body is built.
    body = EntityEntityCreate(
        definition=refs.definition_iri(sess, definition) if definition else None,
        data=EntityEntityCreateData.from_dict(document),
        private=private,
    )
    if slug:
        body.slug = slug
    if parent:
        body.parent = refs.entity_iri(sess, parent)

    created = payload(api_entities_post.sync_detailed(client=sess.require_auth(), body=body), resource=RESOURCE)
    _report_saved(created, "Created", as_json=as_json)


@app.command("edit")
def edit_entity(
    ctx: typer.Context,
    ref: Annotated[str, typer.Argument(help="Entity slug, id or IRI.", metavar="ENTITY")],
    data: DataOption = None,
    field: FieldOption = None,
    parent: ParentOption = None,
    slug: SlugOption = None,
    private: Annotated[
        bool | None,
        typer.Option("--private/--public", help="Change who can see the entity."),
    ] = None,
    as_json: JsonFlag = False,
) -> None:
    """Update an entity.

    With no --data, --field or other change, the entity's data document opens in
    $EDITOR. The data document is replaced wholesale by what you supply, so pass
    the full document rather than only the keys that changed.
    """
    sess = session(ctx)
    entity_id = refs.resolve_entity(sess, ref)
    document = jsonarg.read_json(data, field, what="entity data")

    has_other_change = slug is not None or parent is not None or private is not None
    if document is None and not has_other_change:
        current = _fetch(sess, entity_id)
        document = jsonarg.edit_json(current.get("data") or {}, what="entity data")

    # private defaults to False rather than UNSET in the generated model, so an
    # untouched body would serialise "private": false and quietly publish a
    # private entity on any edit. Start from UNSET and only set it on request.
    body = EntityEntityUpdateJsonMergePatch(private=UNSET)
    if document is not None:
        body.data = EntityEntityUpdateJsonMergePatchData.from_dict(document)
    if slug is not None:
        body.slug = slug
    if parent is not None:
        body.parent = refs.entity_iri(sess, parent)
    if private is not None:
        body.private = private

    updated = payload(
        api_entities_id_patch.sync_detailed(entity_id, client=sess.require_auth(), body=body),
        resource=RESOURCE,
    )
    _report_saved(updated, "Updated", as_json=as_json)


@app.command("delete")
def delete_entity(
    ctx: typer.Context,
    ref: Annotated[str, typer.Argument(help="Entity slug, id or IRI.", metavar="ENTITY")],
    yes: YesFlag = False,
) -> None:
    """Delete an entity. This cannot be undone."""
    sess = session(ctx)
    entity_id = refs.resolve_entity(sess, ref)
    confirm(f"Permanently delete entity {entity_id}?", assume_yes=yes)
    check(api_entities_id_delete.sync_detailed(entity_id, client=sess.require_auth()), resource=RESOURCE)
    output.success(f"Deleted entity {entity_id}")


def _fetch(sess: Session, ref: str) -> dict[str, Any]:
    entity_id = refs.resolve_entity(sess, ref)
    return payload(api_entities_id_get.sync_detailed(entity_id, client=sess.client), resource=RESOURCE)


def _report_saved(entity: dict[str, Any], verb: str, *, as_json: bool) -> None:
    if as_json:
        output.print_json(entity)
        return
    entity_id = refs.resource_id(entity)
    slug = entity.get("slug")
    output.success(f"{verb} entity {entity_id}" + (f" ({slug})" if slug else ""))
    output.print_json(entity.get("data") or {})


def _print_count(result: hal.Page) -> None:
    if result.total_items is None:
        return
    shown = len(result.items)
    suffix = f" of {result.total_items}" if result.total_items != shown else ""
    output.note(f"Showing {shown} entit{'y' if shown == 1 else 'ies'}{suffix}")
