"""Reading JSON payloads from the command line.

Three interchangeable sources, so scripts and humans can each use what suits:

* ``--data '{"name": "foo"}'`` -- inline JSON;
* ``--data @payload.json`` / ``--data -`` -- a file, or stdin;
* ``--field name=foo --field count=3`` -- shorthand key/value pairs, each value
  parsed as JSON when it looks like JSON and kept as a string otherwise.

``--field`` may be combined with ``--data``; the fields win, which makes
"take this file but override one key" a one-liner. Dotted names build nested
objects (``--field owner.name=ada``).
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

from .errors import JsonHubCliError

STDIN_MARKER = "-"
FILE_PREFIX = "@"
DEFAULT_EDITOR = "vi"


class InputError(JsonHubCliError):
    """The user's JSON payload could not be read or parsed."""

    exit_code = 2


def read_json(
    data: str | None,
    fields: list[str] | None = None,
    *,
    what: str = "data",
) -> dict[str, Any] | None:
    """Build a JSON object from ``--data`` and ``--field``.

    Returns ``None`` when neither was supplied, so callers can distinguish
    "no payload given" from "an explicitly empty payload" (``--data '{}'``).
    """
    document: dict[str, Any] | None = None
    if data is not None:
        document = _parse_object(_read_source(data, what=what), what=what)
    for spec in fields or []:
        if document is None:
            document = {}
        name, value = _parse_field(spec)
        _assign(document, name, value)
    return document


def require_json(data: str | None, fields: list[str] | None = None, *, what: str = "data") -> dict[str, Any]:
    """Like :func:`read_json`, but insist on a payload."""
    document = read_json(data, fields, what=what)
    if document is None:
        raise InputError(
            f"no {what} given",
            hint="pass --data '{...}', --data @file.json, --data - to read stdin, or --field key=value",
        )
    return document


def edit_json(initial: dict[str, Any], *, what: str = "data") -> dict[str, Any]:
    """Open ``$VISUAL``/``$EDITOR`` on ``initial`` and parse what comes back."""
    editor = os.environ.get("VISUAL") or os.environ.get("EDITOR") or DEFAULT_EDITOR
    # $EDITOR routinely carries flags ("code --wait", "emacs -nw") and may
    # quote them, so split it the way a shell would rather than on whitespace.
    try:
        argv = shlex.split(editor)
    except ValueError as exc:
        raise InputError(f"could not read $EDITOR ('{editor}'): {exc}") from exc
    if not argv:
        raise InputError("$EDITOR is set but empty")
    with tempfile.NamedTemporaryFile("w+", suffix=".json", delete=False) as handle:
        json.dump(initial, handle, indent=2, ensure_ascii=False, default=str)
        handle.write("\n")
        path = Path(handle.name)
    try:
        try:
            completed = subprocess.run([*argv, str(path)], check=False)
        except OSError as exc:
            raise InputError(f"could not launch editor '{editor}': {exc}") from exc
        if completed.returncode != 0:
            raise InputError(f"editor '{editor}' exited with status {completed.returncode}; nothing was changed")
        text = path.read_text()
    finally:
        path.unlink(missing_ok=True)
    if not text.strip():
        raise InputError(f"the {what} was left empty; nothing was changed")
    return _parse_object(text, what=what)


def _read_source(data: str, *, what: str) -> str:
    if data == STDIN_MARKER:
        if sys.stdin.isatty():
            raise InputError(f"reading {what} from stdin, but stdin is a terminal")
        return sys.stdin.read()
    if data.startswith(FILE_PREFIX):
        path = Path(data[1:]).expanduser()
        try:
            return path.read_text()
        except OSError as exc:
            raise InputError(f"cannot read {what} from {path}: {exc}") from exc
    return data


def _parse_object(text: str, *, what: str) -> dict[str, Any]:
    if not text.strip():
        raise InputError(f"the {what} is empty")
    try:
        document = json.loads(text)
    except json.JSONDecodeError as exc:
        raise InputError(f"the {what} is not valid JSON: {exc.msg} (line {exc.lineno}, column {exc.colno})") from exc
    if not isinstance(document, dict):
        raise InputError(f"the {what} must be a JSON object, got {type(document).__name__}")
    return document


def _parse_field(spec: str) -> tuple[str, Any]:
    name, separator, raw = spec.partition("=")
    if not separator or not name:
        raise InputError(f"--field expects 'name=value', got '{spec}'")
    try:
        # Numbers, booleans, null, arrays and objects come through typed;
        # anything else (including bare words) stays a string.
        return name, json.loads(raw)
    except json.JSONDecodeError:
        return name, raw


def _assign(document: dict[str, Any], name: str, value: Any) -> None:
    """Set ``name`` on ``document``, treating dots as nesting."""
    *parents, leaf = name.split(".")
    cursor = document
    for part in parents:
        existing = cursor.get(part)
        if not isinstance(existing, dict):
            existing = {}
            cursor[part] = existing
        cursor = existing
    cursor[leaf] = value
