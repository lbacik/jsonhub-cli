"""Human and machine output.

Every command renders through this module so that ``--json`` is uniform: with
the flag set, exactly the API's JSON goes to stdout and nothing else, making
``jsonhub ... --json | jq`` reliable. Progress notes, prompts and warnings all
go to stderr for the same reason.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any

from rich.console import Console
from rich.json import JSON
from rich.table import Table
from rich.text import Text

#: stdout - results only.
out = Console(highlight=False, soft_wrap=False)
#: stderr - diagnostics, prompts, warnings. Never parsed by callers.
err = Console(stderr=True, highlight=False)

TRUNCATED_SUFFIX = "..."


def is_tty() -> bool:
    return sys.stdout.isatty()


def print_json(data: Any) -> None:
    """Emit a JSON document on stdout, pretty-printed only for a terminal."""
    if is_tty():
        out.print(JSON(json.dumps(data, ensure_ascii=False, default=str), indent=2))
    else:
        print(json.dumps(data, ensure_ascii=False, indent=2, default=str))


@dataclass(frozen=True)
class Col:
    """A table column with width constraints.

    Rich shrinks every flexible column proportionally when a table exceeds the
    terminal width, which would elide a slug to keep room for a JSON preview.
    ``min_width`` protects identity columns and ``max_width`` stops preview
    columns from claiming space they do not need.
    """

    name: str
    min_width: int | None = None
    max_width: int | None = None


def print_table(
    columns: Sequence[str | Col],
    rows: Iterable[Sequence[Any]],
    *,
    empty: str = "No results",
) -> None:
    """Render rows as a table, or as bare TSV when piped.

    Piped output drops the box drawing and the header so it composes with
    ``cut``/``awk`` the way ``gh`` does.
    """
    materialised = [list(row) for row in rows]
    if not materialised:
        err.print(f"[dim]{empty}[/dim]")
        return

    if not is_tty():
        for row in materialised:
            print("\t".join(_flatten(cell) for cell in row))
        return

    # One line per row, like gh: a wrapped 36-character UUID is harder to read
    # than an elided title, and piped output above always carries full values.
    table = Table(box=None, pad_edge=False, header_style="bold", show_edge=False)
    for column in columns:
        spec = Col(column) if isinstance(column, str) else column
        table.add_column(
            spec.name,
            no_wrap=True,
            overflow="ellipsis",
            min_width=spec.min_width,
            max_width=spec.max_width,
        )
    for row in materialised:
        table.add_row(*(_cell(value) for value in row))
    out.print(table)


def print_fields(fields: Sequence[tuple[str, Any]]) -> None:
    """Render a single resource as an aligned label/value list."""
    if not is_tty():
        for label, value in fields:
            print(f"{label}\t{_flatten(value)}")
        return
    table = Table(box=None, show_header=False, pad_edge=False, show_edge=False)
    table.add_column(style="dim", justify="right")
    table.add_column(overflow="fold")
    for label, value in fields:
        table.add_row(label, _cell(value))
    out.print(table)


def success(message: str) -> None:
    err.print(f"[green]✓[/green] {message}")


def warn(message: str) -> None:
    err.print(f"[yellow]![/yellow] {message}")


def note(message: str) -> None:
    err.print(f"[dim]{message}[/dim]")


def fail(message: str, *, hint: str | None = None) -> None:
    err.print(f"[red]✗[/red] {message}")
    if hint:
        err.print(f"  [dim]{hint}[/dim]")


def truncate(value: str, width: int) -> str:
    """Shorten a string for table display, keeping it under ``width`` chars."""
    if width <= len(TRUNCATED_SUFFIX) or len(value) <= width:
        return value
    return value[: width - len(TRUNCATED_SUFFIX)] + TRUNCATED_SUFFIX


def summarize_json(value: Any, width: int = 60) -> str:
    """One-line preview of an arbitrary JSON value, for a table cell."""
    if value is None:
        return ""
    if isinstance(value, str):
        return truncate(value, width)
    rendered = json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)
    return truncate(rendered, width)


def _cell(value: Any) -> str | Text:
    if value is None:
        return Text("-", style="dim")
    if isinstance(value, bool):
        return Text("yes" if value else "no", style="" if value else "dim")
    if isinstance(value, str):
        return value if value else Text("-", style="dim")
    if isinstance(value, Text):
        return value
    return summarize_json(value)


def _flatten(value: Any) -> str:
    """Single-line, tab-free rendering for machine-readable output."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, Text):
        text = value.plain
    elif isinstance(value, str):
        text = value
    else:
        text = json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)
    return text.replace("\t", " ").replace("\n", " ")
