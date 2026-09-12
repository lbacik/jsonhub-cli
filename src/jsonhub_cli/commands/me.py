"""``jsonhub me`` - who the credentials belong to, and their quota usage."""

from __future__ import annotations

from typing import Any

import typer
from jsonhub_sdk.api.user import api_usersme_get

from .. import output
from ..api import payload
from ._shared import JsonFlag, session

LIMIT_LABELS = {
    "entities": "entities",
    "privateEntities": "private entities",
    "definitions": "definitions",
}


def me(ctx: typer.Context, as_json: JsonFlag = False) -> None:
    """Show your account and your quota usage."""
    sess = session(ctx)
    account = payload(api_usersme_get.sync_detailed(client=sess.require_auth()), resource="account")

    if as_json:
        output.print_json(account)
        return

    output.print_fields([("id", account.get("id")), ("email", account.get("email"))])

    limits = account.get("limits")
    if not isinstance(limits, dict):
        return

    output.err.print()
    output.print_table(
        ["RESOURCE", "USED", "LIMIT", "REMAINING"],
        ([LIMIT_LABELS.get(key, key), used, limit, _remaining(used, limit)] for key, used, limit in _rows(limits)),
        empty="The server reported no limits",
    )


def _rows(limits: dict[str, Any]) -> list[tuple[str, Any, Any]]:
    """Flatten ``{name: {used, limit}}``, keeping the documented order first."""
    ordered = [key for key in LIMIT_LABELS if key in limits]
    ordered += [key for key in limits if key not in LIMIT_LABELS]
    rows: list[tuple[str, Any, Any]] = []
    for key in ordered:
        usage = limits.get(key)
        if isinstance(usage, dict):
            rows.append((key, usage.get("used"), usage.get("limit")))
    return rows


def _remaining(used: Any, limit: Any) -> Any:
    if isinstance(used, int) and isinstance(limit, int):
        return max(limit - used, 0)
    return None
