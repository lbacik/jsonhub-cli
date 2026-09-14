"""Best-effort, non-blocking completion for the interactive shell."""

from __future__ import annotations

import shlex
import time
from collections.abc import Callable, Iterable
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Any, Literal

from prompt_toolkit.completion import Completer, Completion
from prompt_toolkit.document import Document
from typer.main import get_command

from . import collection, hal
from .api import Session
from .commands._shared import CliState
from .refs import resource_id

CompletionKind = Literal["entity", "definition"]
FetchCandidates = Callable[[CompletionKind, str | None], list[str]]

CACHE_SECONDS = 30.0
LOOKUP_TIMEOUT = 2.0
LOOKUP_LIMIT = 50


class ShellCompleter(Completer):
    """Complete CLI syntax immediately and API candidates when cached.

    API work is scheduled in a single background worker. A miss returns no
    dynamic suggestions rather than holding up typing; a later completion gets
    the result if it arrived successfully.
    """

    def __init__(self, state: CliState, *, fetch: FetchCandidates | None = None) -> None:
        self.state = state
        self.fetch = fetch or self._fetch_candidates
        self._cache: dict[tuple[CompletionKind, str | None], tuple[float, list[str]]] = {}
        self._pending: dict[tuple[CompletionKind, str | None], Future[list[str]]] = {}
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="jsonhub-completion")

    def get_completions(self, document: Document, complete_event: object) -> Iterable[Completion]:
        del complete_event
        argv, prefix = _completion_words(document.text_before_cursor)
        if argv is None:
            return ()
        candidates = self._candidates(argv, prefix)
        return (
            Completion(candidate, start_position=-len(prefix))
            for candidate in sorted(candidates)
            if candidate.startswith(prefix)
        )

    def invalidate(self) -> None:
        """Forget dynamic candidates after state-changing shell commands."""
        self._cache.clear()
        for future in self._pending.values():
            future.cancel()
        self._pending.clear()

    def close(self) -> None:
        self._executor.shutdown(wait=False, cancel_futures=True)

    def wait_for_pending(self) -> None:
        """Testing hook: wait for the currently scheduled lookup."""
        for future in tuple(self._pending.values()):
            future.result(timeout=5)

    def _candidates(self, argv: list[str], prefix: str) -> list[str]:
        if not argv:
            return [*self._root_commands(), *self._root_options(), "cd", "help", "list", "pwd", "use"]
        if argv[-1] == "--host":
            return list(self.state.config.hosts)
        if argv[0] == "use":
            return list(self.state.config.hosts)
        if argv[0] == "cd":
            return self._dynamic("entity", self.state.current_entity_id)
        if argv[0] == "list":
            return ["definitions", "entities", "--json", "--limit"]

        reference = _reference_kind(argv, prefix)
        if reference is not None:
            return self._dynamic(reference, self.state.current_entity_id)
        return self._syntax_candidates(argv)

    def _root_commands(self) -> list[str]:
        from .main import app

        command = get_command(app)
        return list(getattr(command, "commands", {}))

    def _root_options(self) -> list[str]:
        from .main import app

        command = get_command(app)
        return [option for parameter in command.params for option in parameter.opts]

    def _syntax_candidates(self, argv: list[str]) -> list[str]:
        from .main import app

        command: Any = get_command(app)
        consumed = list(argv)
        while consumed and consumed[0] in getattr(command, "commands", {}):
            command = command.commands[consumed.pop(0)]
        if getattr(command, "commands", None) and not consumed:
            return list(command.commands)
        return [option for parameter in command.params for option in parameter.opts]

    def _dynamic(self, kind: CompletionKind, parent: str | None) -> list[str]:
        key = (kind, parent)
        cached = self._cache.get(key)
        if cached is not None and time.monotonic() - cached[0] < CACHE_SECONDS:
            return cached[1]
        future = self._pending.get(key)
        if future is None:
            self._pending[key] = self._executor.submit(self.fetch, kind, parent)
            return []
        if not future.done():
            return []
        self._pending.pop(key, None)
        try:
            candidates = future.result()
        except Exception:
            return []
        self._cache[key] = (time.monotonic(), candidates)
        return candidates

    def _fetch_candidates(self, kind: CompletionKind, parent: str | None) -> list[str]:
        """Fetch a small candidate set using a short-lived client and timeout."""
        from jsonhub_sdk.api.definition import api_definitions_get_collection
        from jsonhub_sdk.api.entity import api_entities_get_collection
        from jsonhub_sdk.types import UNSET

        session = Session(self.state.config, self.state.host, insecure=self.state.insecure, timeout=LOOKUP_TIMEOUT)
        try:
            endpoint = api_entities_get_collection if kind == "entity" else api_definitions_get_collection
            kwargs: dict[str, Any] = {"client": session.client, "limit": LOOKUP_LIMIT}
            if kind == "entity":
                kwargs["parent"] = parent if parent is not None else UNSET
                kwargs["root"] = True if parent is None else UNSET
            else:
                kwargs["parent_entity"] = parent if parent is not None else UNSET
            body = collection.fetch(lambda: endpoint.sync_detailed(**kwargs), resource=f"{kind} collection")
            return [
                slug for item in hal.items(body) if isinstance((slug := item.get("slug")), str) and resource_id(item)
            ]
        finally:
            session.close()


def _completion_words(text: str) -> tuple[list[str] | None, str]:
    """Split a partial POSIX line, keeping the unfinished word as its prefix."""
    if not text.strip():
        return [], ""
    try:
        trailing_space = text[-1].isspace()
        words = shlex.split(text, posix=True)
    except ValueError:
        return None, ""
    if trailing_space:
        return words, ""
    return words[:-1], words[-1]


def _reference_kind(argv: list[str], prefix: str) -> CompletionKind | None:
    """Return a resource kind only where the CLI accepts a resource reference."""
    words = [*argv, prefix]
    if len(words) >= 3 and words[0] in {"entity", "definition"} and words[1] in {"get", "edit", "delete"}:
        return words[0]  # type: ignore[return-value]
    if len(words) >= 3 and words[-2] in {"--parent", "-P", "--parent-entity"}:
        return "entity"
    if len(words) >= 3 and words[-2] in {"--definition", "-D"}:
        return "definition"
    return None
