"""TTY-only dispatch loop for existing ``jsonhub`` commands.

The shell intentionally owns only terminal concerns.  It parses one line at a
time and forwards the resulting argv to :func:`jsonhub_cli.main.run`, retaining
the normal Typer tree and its established error boundary.
"""

from __future__ import annotations

import shlex
import sys
from collections.abc import Callable, Iterable
from typing import Any

from prompt_toolkit import PromptSession
from prompt_toolkit.history import History
from prompt_toolkit.input.defaults import create_input
from prompt_toolkit.output.defaults import create_output

from . import output
from .errors import JsonHubCliError

Dispatch = Callable[[list[str]], int]


class InteractiveError(JsonHubCliError):
    """The interactive shell cannot safely start or accept a command."""

    exit_code = 2


class SecretSafeHistory(History):
    """In-memory history which omits commands that may contain credentials."""

    def __init__(self) -> None:
        super().__init__()
        self.entries: list[str] = []

    def load_history_strings(self) -> Iterable[str]:
        return reversed(self.entries)

    def store_string(self, string: str) -> None:
        if not _contains_secret(string):
            self.entries.append(string)


def _contains_secret(line: str) -> bool:
    try:
        argv = shlex.split(line, posix=True)
    except ValueError:
        return False
    return _is_token_login(argv)


def parse(line: str) -> list[str] | None:
    """Parse a shell line, reporting unmatched quotes as a line-level error."""
    if not line.strip():
        return None
    try:
        argv = shlex.split(line, posix=True)
    except ValueError as exc:
        output.fail(f"could not parse command: {exc}")
        return None
    if argv and argv[0] == "help":
        return [*argv[1:], "--help"]
    return argv


def validate(argv: list[str]) -> str | None:
    """Return an explanation for a command that cannot run inside the shell."""
    if "--interactive" in argv:
        return "interactive mode is already running"
    if "--data=-" in argv or "-d-" in argv:
        return "--data - requires exclusive stdin; run this command outside interactive mode"
    if any(option in argv for option in ("--data", "-d")):
        for index, option in enumerate(argv[:-1]):
            if option in {"--data", "-d"} and argv[index + 1] == "-":
                return "--data - requires exclusive stdin; run this command outside interactive mode"
    if _is_token_login(argv):
        return "auth login --with-token requires exclusive stdin; run it outside interactive mode"
    return None


class InteractiveSession:
    """A single, non-persistent terminal session over a command dispatcher."""

    def __init__(
        self,
        dispatch: Dispatch,
        *,
        stdin: Any = None,
        stdout: Any = None,
        prompt_factory: Callable[..., Any] = PromptSession,
        input_factory: Callable[..., Any] = create_input,
        output_factory: Callable[..., Any] = create_output,
    ) -> None:
        self.dispatch = dispatch
        self.stdin = sys.stdin if stdin is None else stdin
        self.stdout = sys.stdout if stdout is None else stdout
        self.prompt_factory = prompt_factory
        self.input_factory = input_factory
        self.output_factory = output_factory
        self.history = SecretSafeHistory()

    def run(self) -> None:
        """Prompt until EOF, leaving command failures isolated to their line."""
        if not _is_tty(self.stdin) or not _is_tty(self.stdout):
            raise InteractiveError("--interactive requires both stdin and stdout to be terminals")

        prompt = self.prompt_factory(
            history=self.history,
            input=self.input_factory(self.stdin),
            output=self.output_factory(sys.stderr, always_prefer_tty=False),
        )
        while True:
            try:
                line = prompt.prompt("jsonhub> ")
            except KeyboardInterrupt:
                output.note("Input cancelled.")
                continue
            except EOFError:
                output.note("Leaving interactive mode.")
                return

            argv = parse(line)
            if argv is None:
                continue
            if message := validate(argv):
                output.fail(message)
                continue
            try:
                self.dispatch(argv)
            except KeyboardInterrupt:
                output.note("Command cancelled.")


def start(dispatch: Dispatch) -> None:
    """Start the default prompt-toolkit session."""
    InteractiveSession(dispatch).run()


def _is_tty(stream: Any) -> bool:
    """Treat streams without ``isatty`` as non-interactive."""
    isatty = getattr(stream, "isatty", None)
    return bool(isatty and isatty())


def _is_token_login(argv: list[str]) -> bool:
    """Recognise token login even when root options precede the command."""
    is_login = any(argv[index : index + 2] == ["auth", "login"] for index in range(len(argv) - 1))
    return "--with-token" in argv and is_login
