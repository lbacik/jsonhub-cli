"""Turning what a user types into what the API wants.

JsonHub identifies resources by UUID but also gives most of them a slug, and
relations (``definition``, ``parent``, ``parentEntity``) are written as IRI
references such as ``/api/definitions/<uuid>``. Users should be able to type
whichever they have -- a UUID, a slug, a full IRI, or a URL copied from the web
app -- so every command funnels its arguments through here.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit
from uuid import UUID

from jsonhub_sdk.api.definition import api_definitions_get_collection
from jsonhub_sdk.api.entity import api_entities_get_collection

from . import collection, hal
from .api import Session
from .errors import JsonHubCliError, NotFoundError

ENTITY_PATH = "/api/entities"
DEFINITION_PATH = "/api/definitions"

#: How many partial-match candidates to look at when resolving a slug.
SLUG_LOOKUP_LIMIT = 50


class AmbiguousRefError(JsonHubCliError):
    """A slug matched more than one resource, so we refuse to guess."""

    exit_code = 2


def is_uuid(value: str) -> bool:
    try:
        UUID(value)
    except ValueError:
        return False
    return True


def iri(path: str, resource_id: str) -> str:
    """Build the IRI reference the API expects for a relation field."""
    return f"{path}/{resource_id}"


def id_from_iri(value: str) -> str | None:
    """Pull the trailing identifier out of an IRI, URL or bare path."""
    candidate = urlsplit(value).path.rstrip("/")
    if "/" not in candidate:
        return None
    tail = candidate.rsplit("/", 1)[1]
    return tail or None


def resource_id(resource: dict[str, Any]) -> str | None:
    """Read a resource's id, preferring the explicit field over ``_links.self``."""
    value = resource.get("id")
    if isinstance(value, str) and value:
        return value
    links = resource.get("_links")
    if isinstance(links, dict):
        self_link = links.get("self")
        if isinstance(self_link, dict):
            href = self_link.get("href")
            if isinstance(href, str):
                return id_from_iri(href)
    return None


def resolve_entity(session: Session, ref: str) -> str:
    """Resolve a user-supplied entity reference to a UUID."""
    return _resolve(session, ref, kind="entity")


def resolve_definition(session: Session, ref: str) -> str:
    """Resolve a user-supplied definition reference to a UUID."""
    return _resolve(session, ref, kind="definition")


def entity_iri(session: Session, ref: str) -> str:
    return iri(ENTITY_PATH, resolve_entity(session, ref))


def definition_iri(session: Session, ref: str) -> str:
    return iri(DEFINITION_PATH, resolve_definition(session, ref))


def _resolve(session: Session, ref: str, *, kind: str) -> str:
    ref = ref.strip()
    if not ref:
        raise NotFoundError(f"empty {kind} reference")
    if is_uuid(ref):
        return ref
    # A pasted IRI or web-app URL: trust its trailing segment if it is a UUID,
    # otherwise treat that segment as a slug.
    if "/" in ref:
        tail = id_from_iri(ref)
        if tail and is_uuid(tail):
            return tail
        if tail:
            ref = tail
    return _resolve_slug(session, ref, kind=kind)


def _resolve_slug(session: Session, slug: str, *, kind: str) -> str:
    """Find a resource by exact slug, using the API's partial-match filter.

    ``qid`` matches substrings, so the candidates are filtered down to exact
    slug equality here; a substring-only hit is not a match.
    """
    endpoint = api_entities_get_collection if kind == "entity" else api_definitions_get_collection
    body = collection.fetch(
        lambda: endpoint.sync_detailed(client=session.client, qid=slug, limit=SLUG_LOOKUP_LIMIT),
        resource=f"{kind} collection",
    )
    candidates = hal.items(body)
    exact = [c for c in candidates if c.get("slug") == slug]
    if not exact:
        raise NotFoundError(
            f"no {kind} with slug '{slug}'",
            hint=f"list what is available with 'jsonhub {kind} list --search {slug}'",
        )
    ids = [rid for rid in (resource_id(c) for c in exact) if rid]
    if not ids:
        raise NotFoundError(f"the {kind} matching slug '{slug}' has no usable id")
    if len(set(ids)) > 1:
        listed = ", ".join(sorted(set(ids))[:5])
        raise AmbiguousRefError(
            f"slug '{slug}' matches {len(set(ids))} {kind}s",
            hint=f"pass one of these ids instead: {listed}",
        )
    return ids[0]


def relation(resource: dict[str, Any], name: str) -> dict[str, Any] | None:
    """Read an embedded relation such as ``definition`` or ``parent``.

    HAL puts the related resource under ``_embedded`` and its IRI under
    ``_links``, but a flattened ``application/json`` representation puts the
    object at the top level, so all three shapes are accepted.
    """
    direct = resource.get(name)
    if isinstance(direct, dict):
        return direct
    embedded = resource.get("_embedded")
    if isinstance(embedded, dict):
        nested = embedded.get(name)
        if isinstance(nested, dict):
            return nested
    if isinstance(direct, str) and direct:
        # Only an IRI is available; synthesise the minimal shape.
        related_id = id_from_iri(direct)
        return {"id": related_id} if related_id else None
    links = resource.get("_links")
    if isinstance(links, dict) and isinstance(links.get(name), dict):
        href = links[name].get("href")
        if isinstance(href, str):
            related_id = id_from_iri(href)
            return {"id": related_id} if related_id else None
    return None


def relation_id(resource: dict[str, Any], name: str) -> str | None:
    related = relation(resource, name)
    return resource_id(related) if related else None


def relation_label(resource: dict[str, Any], name: str) -> str | None:
    """Human-facing name for a relation: its slug, else its id."""
    related = relation(resource, name)
    if not related:
        return None
    slug = related.get("slug")
    if isinstance(slug, str) and slug:
        return slug
    return resource_id(related)
