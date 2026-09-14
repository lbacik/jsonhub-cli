"""Interactive adapter and terminal-session behavior."""

from __future__ import annotations

from collections.abc import Callable, Iterator

import pytest

from jsonhub_cli.interactive import InteractiveError, InteractiveSession, SecretSafeHistory, parse, validate

from .conftest import Result


class Tty:
    def isatty(self) -> bool:
        return True


class Prompts:
    def __init__(self, responses: Iterator[str | BaseException]) -> None:
        self.responses = responses

    def prompt(self, _: str) -> str:
        response = next(self.responses)
        if isinstance(response, BaseException):
            raise response
        return response


def test_parse_handles_blank_help_and_unmatched_quotes(capsys: pytest.CaptureFixture[str]) -> None:
    assert parse("   ") is None
    assert parse("help entity") == ["entity", "--help"]
    assert parse("entity get 'one two'") == ["entity", "get", "one two"]
    assert parse("entity get '") is None
    assert "could not parse command" in capsys.readouterr().err


def test_validate_rejects_nested_and_exclusive_stdin_commands() -> None:
    assert validate(["entity", "list", "--interactive"]) == "interactive mode is already running"
    assert "exclusive stdin" in (validate(["entity", "create", "--data", "-"]) or "")
    assert "exclusive stdin" in (validate(["entity", "create", "--data=-"]) or "")
    assert "exclusive stdin" in (validate(["--host", "example.test", "auth", "login", "--with-token"]) or "")
    assert validate(["entity", "list"]) is None


def test_history_is_memory_only_and_omits_token_login() -> None:
    history = SecretSafeHistory()

    history.store_string("entity list")
    history.store_string("--host example.test auth login --with-token")

    assert history.entries == ["entity list"]
    assert list(history.load_history_strings()) == ["entity list"]


def test_session_keeps_running_after_input_and_command_interrupts(capsys: pytest.CaptureFixture[str]) -> None:
    calls: list[list[str]] = []
    responses: Iterator[str | BaseException] = iter(
        [KeyboardInterrupt(), "", "entity list", "definition list", EOFError()]
    )

    def dispatch(argv: list[str]) -> int:
        calls.append(argv)
        if argv == ["entity", "list"]:
            raise KeyboardInterrupt
        return 0

    session = InteractiveSession(
        dispatch,
        stdin=Tty(),
        stdout=Tty(),
        prompt_factory=lambda **_: Prompts(responses),
        input_factory=lambda _: object(),
        output_factory=lambda *_args, **_kwargs: object(),
    )

    session.run()

    assert calls == [["entity", "list"], ["definition", "list"]]
    assert "Input cancelled." in capsys.readouterr().err


def test_session_continues_after_a_command_error() -> None:
    calls: list[list[str]] = []
    responses: Iterator[str | BaseException] = iter(["entity list", "--version", EOFError()])

    def dispatch(argv: list[str]) -> int:
        calls.append(argv)
        return 1 if argv == ["entity", "list"] else 0

    session = InteractiveSession(
        dispatch,
        stdin=Tty(),
        stdout=Tty(),
        prompt_factory=lambda **_: Prompts(responses),
        input_factory=lambda _: object(),
        output_factory=lambda *_args, **_kwargs: object(),
    )

    session.run()

    assert calls == [["entity", "list"], ["--version"]]


def test_session_rejects_non_tty() -> None:
    session = InteractiveSession(lambda _: 0, stdin=object(), stdout=Tty())

    with pytest.raises(InteractiveError, match="requires both stdin and stdout"):
        session.run()


def test_root_interactive_guard_uses_the_standard_error_boundary(invoke: Callable[..., Result]) -> None:
    result = invoke("--interactive")

    assert isinstance(result, Result)
    assert result.exit_code == 2
    assert "requires both stdin and stdout" in result.stderr
