"""``jsonhub token`` - manage personal access tokens for the current account.

These are the long-lived credentials for CI and scripts. A token's secret is
returned exactly once, at creation; afterwards only a preview is available.
"""

from __future__ import annotations

import datetime as dt
from typing import Annotated, Any

import typer
from jsonhub_sdk.api.personal_access_token import (
    api_meapi_tokens_get_collection,
    api_meapi_tokens_id_delete,
    api_meapi_tokens_id_patch,
    api_meapi_tokens_post,
)
from jsonhub_sdk.models import (
    PersonalAccessTokenPersonalAccessTokenWrite,
    PersonalAccessTokenPersonalAccessTokenWriteJsonMergePatch,
)

from .. import collection, hal, output, paging, refs
from ..api import check, payload
from ..errors import JsonHubCliError
from ._shared import JsonFlag, LimitOption, PageOption, YesFlag, confirm, session

app = typer.Typer(no_args_is_help=True, help="Manage personal access tokens for your account.")

RESOURCE = "personal access token"

ExpiresOption = Annotated[
    str | None,
    typer.Option(
        "--expires",
        help="Expiry as an ISO-8601 timestamp (2027-01-31, 2027-01-31T12:00:00Z) or a day count (90d).",
        metavar="WHEN",
    ),
]


@app.command("list")
def list_tokens(
    ctx: typer.Context,
    limit: LimitOption = 30,
    page: PageOption = None,
    as_json: JsonFlag = False,
) -> None:
    """List your personal access tokens."""
    sess = session(ctx)
    client = sess.require_auth()

    def fetch(page_number: int, page_size: int) -> dict[str, Any]:
        return collection.fetch(
            lambda: api_meapi_tokens_get_collection.sync_detailed(client=client, page=page_number, limit=page_size),
            resource="token collection",
        )

    result = paging.collect(fetch, limit=limit, page=page)

    if as_json:
        output.print_json(result.items)
        return

    output.print_table(
        ["ID", "NAME", "PREVIEW", "CREATED", "EXPIRES"],
        (
            [
                refs.resource_id(item),
                item.get("name"),
                item.get("tokenPreview"),
                _date(item.get("createdAt")),
                _date(item.get("expiresAt")) or "never",
            ]
            for item in result.items
        ),
        empty="No personal access tokens",
    )
    _print_count(result)


@app.command("create")
def create_token(
    ctx: typer.Context,
    name: Annotated[str, typer.Argument(help="What this token is for, e.g. 'ci-deploy'.")],
    expires: ExpiresOption = None,
    as_json: JsonFlag = False,
) -> None:
    """Create a personal access token and print it once.

    The secret is shown only here -- JsonHub never returns it again -- so pipe
    it somewhere safe:

        jsonhub token create ci --json | jq -r .token
    """
    sess = session(ctx)
    body = PersonalAccessTokenPersonalAccessTokenWrite(name=name)
    if expires:
        body.expires_at = _parse_expiry(expires)

    created = payload(api_meapi_tokens_post.sync_detailed(client=sess.require_auth(), body=body), resource=RESOURCE)

    if as_json:
        output.print_json(created)
        return

    secret = created.get("token")
    output.success(f"Created token '{name}' ({refs.resource_id(created)})")
    if isinstance(secret, str) and secret:
        output.warn("This is the only time the token is shown. Store it now.")
        print(secret)
    else:
        output.warn("The server did not return the token secret")


@app.command("rename")
def rename_token(
    ctx: typer.Context,
    ref: Annotated[str, typer.Argument(help="Token id.", metavar="TOKEN")],
    name: Annotated[str, typer.Argument(help="The new name.")],
    as_json: JsonFlag = False,
) -> None:
    """Rename a personal access token."""
    sess = session(ctx)
    body = PersonalAccessTokenPersonalAccessTokenWriteJsonMergePatch(name=name)
    updated = payload(
        api_meapi_tokens_id_patch.sync_detailed(_token_id(ref), client=sess.require_auth(), body=body),
        resource=RESOURCE,
    )
    if as_json:
        output.print_json(updated)
        return
    output.success(f"Renamed token {refs.resource_id(updated)} to '{name}'")


@app.command("delete")
def delete_token(
    ctx: typer.Context,
    ref: Annotated[str, typer.Argument(help="Token id.", metavar="TOKEN")],
    yes: YesFlag = False,
) -> None:
    """Revoke a personal access token. Anything using it stops working."""
    sess = session(ctx)
    token_id = _token_id(ref)
    confirm(f"Permanently revoke token {token_id}?", assume_yes=yes)
    check(api_meapi_tokens_id_delete.sync_detailed(token_id, client=sess.require_auth()), resource=RESOURCE)
    output.success(f"Revoked token {token_id}")


def _token_id(ref: str) -> str:
    """Tokens have no slug, so only ids (or IRIs holding one) are accepted."""
    candidate = refs.id_from_iri(ref) if "/" in ref else ref
    if not candidate or not refs.is_uuid(candidate):
        raise JsonHubCliError(
            f"'{ref}' is not a token id",
            hint="tokens are addressed by id only; find it with 'jsonhub token list'",
        )
    return candidate


def _parse_expiry(value: str) -> dt.datetime:
    """Accept either ``90d`` or an ISO-8601 date/timestamp."""
    text = value.strip()
    if text.endswith("d") and text[:-1].isdigit():
        return dt.datetime.now(dt.UTC) + dt.timedelta(days=int(text[:-1]))
    try:
        parsed = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise JsonHubCliError(
            f"could not read '{value}' as an expiry",
            hint="use an ISO-8601 date (2027-01-31), a timestamp, or a day count (90d)",
        ) from exc
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.UTC)


def _date(value: Any) -> str | None:
    """Render an API timestamp as a plain date; pass anything odd through."""
    if not isinstance(value, str) or not value:
        return None
    try:
        return dt.datetime.fromisoformat(value.replace("Z", "+00:00")).date().isoformat()
    except ValueError:
        return value


def _print_count(result: hal.Page) -> None:
    if result.total_items is None:
        return
    shown = len(result.items)
    suffix = f" of {result.total_items}" if result.total_items != shown else ""
    output.note(f"Showing {shown} token{'' if shown == 1 else 's'}{suffix}")
