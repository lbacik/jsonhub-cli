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

from . import output, refs
from .commands._shared import CliState
from .commands.resource import list_current_resources
from .errors import JsonHubCliError, NotFoundError

Dispatch = Callable[[list[str]], int]


class InteractiveError(JsonHubCliError):
    """The interactive shell cannot safely start or accept a command."""

    exit_code = 2


class StaleLocationError(JsonHubCliError):
    """The entity remembered by a shell session is no longer navigable."""

    exit_code = 3


class ShellDispatcher:
    """Dispatch shell built-ins and normal CLI commands in one durable context."""

    def __init__(self, dispatch: Dispatch, state: CliState) -> None:
        self.dispatch = dispatch
        self.state = state

    def __call__(self, argv: list[str]) -> int:
        if argv[0] == "cd":
            return self._cd(argv)
        if argv[0] == "pwd":
            return self._pwd(argv)
        if argv[0] == "use":
            return self._use(argv)
        if argv[0] == "list":
            return self._list(argv)
        return self.dispatch(argv)

    def _list(self, argv: list[str]) -> int:
        if len(argv) > 1 and argv[1] in {"entities", "definitions"}:
            noun = "entity" if argv[1] == "entities" else "definition"
            return self.dispatch([noun, "list", *argv[2:]])

        limit, as_json = _combined_list_options(argv[1:])
        list_current_resources(self.state, limit=limit, as_json=as_json)
        return 0

    def _cd(self, argv: list[str]) -> int:
        if len(argv) != 2:
            raise InteractiveError("usage: cd PATH")
        path = argv[1]
        if path == "/":
            self.state.current_entity_id = None
            return 0
        target = self._resolve_path(path)
        self.state.current_entity_id = target
        return 0

    def _pwd(self, argv: list[str]) -> int:
        if len(argv) != 1:
            raise InteractiveError("usage: pwd")
        if self.state.current_entity_id is None:
            print("/")
            return 0
        try:
            segments = self._path_segments(self.state.current_entity_id)
        except JsonHubCliError as exc:
            raise StaleLocationError("current location is stale; run 'cd /' to return to root") from exc
        print("/" + "/".join(segments))
        return 0

    def _use(self, argv: list[str]) -> int:
        if len(argv) != 2 or not argv[1].strip():
            raise InteractiveError("usage: use HOST")
        self.state.use_host(argv[1])
        return 0

    def _resolve_path(self, path: str) -> str | None:
        if not path or path.endswith("/") or "//" in path:
            raise InteractiveError("entity paths cannot contain empty segments")
        path_id = refs.id_from_iri(path)
        if refs.is_uuid(path) or "://" in path or (path_id is not None and refs.is_uuid(path_id)):
            raise InteractiveError("navigation paths contain child slugs, not ids or URLs")
        absolute = path.startswith("/")
        segments = path[1:].split("/") if absolute else path.split("/")
        current = None if absolute else self.state.current_entity_id
        for segment in segments:
            if not segment:
                raise InteractiveError("entity paths cannot contain empty segments")
            if segment == ".":
                continue
            if segment == "..":
                if current is not None:
                    current = refs.relation_id(refs.fetch_entity(self.state.session, current), "parent")
                continue
            if refs.is_uuid(segment) or ":" in segment:
                raise InteractiveError("navigation paths contain child slugs, not ids or URLs")
            current = refs.resolve_child_entity(self.state.session, current, segment)
        return current

    def _path_segments(self, entity_id: str) -> list[str]:
        segments: list[str] = []
        seen: set[str] = set()
        current: str | None = entity_id
        while current is not None:
            if current in seen:
                raise StaleLocationError("current location has cyclic ancestry")
            seen.add(current)
            entity = refs.fetch_entity(self.state.session, current)
            slug = entity.get("slug")
            if not isinstance(slug, str) or not slug:
                raise NotFoundError("current location has no slug")
            segments.append(slug)
            current = refs.relation_id(entity, "parent")
        return list(reversed(segments))


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
            except JsonHubCliError as exc:
                # Ordinary CLI commands pass through main.run(), which already
                # renders these errors. Shell built-ins have no Typer command
                # boundary, so render their expected failures here and keep the
                # session usable for the next line.
                output.fail(exc.message, hint=exc.hint)


def start(dispatch: Dispatch, state: CliState | None = None) -> None:
    """Start the default prompt-toolkit session."""
    InteractiveSession(ShellDispatcher(dispatch, state) if state is not None else dispatch).run()


def _is_tty(stream: Any) -> bool:
    """Treat streams without ``isatty`` as non-interactive."""
    isatty = getattr(stream, "isatty", None)
    return bool(isatty and isatty())


def _combined_list_options(argv: list[str]) -> tuple[int, bool]:
    """Parse the intentionally small option surface of the mixed list."""
    limit = 30
    as_json = False
    index = 0
    while index < len(argv):
        option = argv[index]
        if option == "--json":
            as_json = True
        elif option in {"--limit", "-L"}:
            if index + 1 == len(argv):
                raise InteractiveError(f"{option} requires a positive integer")
            index += 1
            try:
                limit = int(argv[index])
            except ValueError as exc:
                raise InteractiveError("--limit requires a positive integer") from exc
            if limit < 1:
                raise InteractiveError("--limit requires a positive integer")
        elif option == "--page" or option.startswith("--page="):
            raise InteractiveError("list does not accept --page; use list entities or list definitions")
        else:
            raise InteractiveError(f"unknown list option: {option}")
        index += 1
    return limit, as_json


def _is_token_login(argv: list[str]) -> bool:
    """Recognise token login even when root options precede the command."""
    is_login = any(argv[index : index + 2] == ["auth", "login"] for index in range(len(argv) - 1))
    return "--with-token" in argv and is_login
