"""Option types and helpers shared by every command group."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Annotated, Any

import typer

from ..api import Session
from ..config import Config
from ..errors import JsonHubCliError

# --- Reusable option annotations ------------------------------------------------
# Declared once so that, for example, --json means the same thing and carries the
# same help text in every command.

JsonFlag = Annotated[
    bool,
    typer.Option("--json", help="Print the raw API JSON instead of a table."),
]

LimitOption = Annotated[
    int,
    typer.Option("--limit", "-L", min=1, help="Maximum number of items to fetch, paginating as needed."),
]

PageOption = Annotated[
    int | None,
    typer.Option("--page", min=1, help="Fetch exactly this one page of --limit items instead of auto-paginating."),
]

SearchOption = Annotated[
    str | None,
    typer.Option("--search", "-q", help="Filter by a partial match on slug or id."),
]

DataOption = Annotated[
    str | None,
    typer.Option(
        "--data",
        "-d",
        help="JSON body: inline, @file.json, or - to read stdin.",
        metavar="JSON",
    ),
]

FieldOption = Annotated[
    list[str] | None,
    typer.Option(
        "--field",
        "-f",
        help="Set one key, e.g. -f name=ada or -f tags='[\"a\"]'. Dots nest. Repeatable.",
        metavar="KEY=VALUE",
    ),
]

YesFlag = Annotated[
    bool,
    typer.Option("--yes", "-y", help="Skip the confirmation prompt."),
]


@dataclass
class CliState:
    """Root-level state that subcommands read out of the Typer context."""

    host: str | None = None
    config: Config = field(default_factory=Config.load)
    _session: Session | None = None

    @property
    def session(self) -> Session:
        if self._session is None:
            self._session = Session(self.config, self.host)
        return self._session


def state(ctx: typer.Context) -> CliState:
    """Fetch the :class:`CliState` the root callback stored on the context."""
    obj = ctx.obj
    if not isinstance(obj, CliState):
        # Only reachable if a command is invoked without the root callback,
        # e.g. from a unit test; building a default state keeps that usable.
        obj = CliState()
        ctx.obj = obj
    return obj


def session(ctx: typer.Context) -> Session:
    return state(ctx).session


def confirm(prompt: str, *, assume_yes: bool) -> None:
    """Ask before doing something destructive, unless ``--yes`` was passed."""
    if assume_yes:
        return
    if not typer.confirm(prompt, default=False, err=True):
        raise typer.Abort


def require_field(resource: dict[str, Any], name: str, *, what: str) -> Any:
    value = resource.get(name)
    if value is None:
        raise JsonHubCliError(f"the {what} returned by the API has no '{name}'")
    return value
