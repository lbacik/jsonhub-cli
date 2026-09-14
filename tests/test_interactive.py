"""Interactive adapter and terminal-session behavior."""

from __future__ import annotations

from collections.abc import Callable, Iterator

import pytest
from pytest_httpx import HTTPXMock

from jsonhub_cli.commands._shared import CliState
from jsonhub_cli.interactive import (
    InteractiveError,
    InteractiveSession,
    SecretSafeHistory,
    ShellDispatcher,
    StaleLocationError,
    parse,
    validate,
)

from .conftest import Result, entity, hal_collection


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


def test_cd_traverses_direct_children_and_pwd_renders_the_full_path(
    httpx_mock: HTTPXMock, capsys: pytest.CaptureFixture[str]
) -> None:
    state = CliState()
    shell = ShellDispatcher(lambda _: 0, state)
    root_id = "10000000-0000-0000-0000-000000000001"
    child_id = "10000000-0000-0000-0000-000000000002"
    httpx_mock.add_response(json=hal_collection(entity(root_id, "root")))
    httpx_mock.add_response(json=hal_collection(entity(child_id, "child")))

    assert shell(["cd", "/root/child"]) == 0
    assert state.current_entity_id == child_id

    child = entity(child_id, "child")
    child["_links"]["parent"] = {"href": f"/api/entities/{root_id}"}
    httpx_mock.add_response(json=child)
    httpx_mock.add_response(json=entity(root_id, "root"))
    assert shell(["pwd"]) == 0

    assert capsys.readouterr().out == "/root/child\n"


def test_failed_cd_keeps_the_previous_location(httpx_mock: HTTPXMock) -> None:
    state = CliState(current_entity_id="10000000-0000-0000-0000-000000000003")
    shell = ShellDispatcher(lambda _: 0, state)
    httpx_mock.add_response(json=hal_collection())

    with pytest.raises(Exception, match="no child entity"):
        shell(["cd", "missing"])

    assert state.current_entity_id == "10000000-0000-0000-0000-000000000003"


def test_cd_supports_relative_parent_and_root_paths(httpx_mock: HTTPXMock) -> None:
    root_id = "10000000-0000-0000-0000-000000000001"
    child_id = "10000000-0000-0000-0000-000000000002"
    grandchild_id = "10000000-0000-0000-0000-000000000003"
    state = CliState(current_entity_id=root_id)
    shell = ShellDispatcher(lambda _: 0, state)
    httpx_mock.add_response(json=hal_collection(entity(child_id, "child")))
    httpx_mock.add_response(json=hal_collection(entity(grandchild_id, "grandchild")))

    assert shell(["cd", "child/grandchild"]) == 0
    assert state.current_entity_id == grandchild_id

    grandchild = entity(grandchild_id, "grandchild")
    grandchild["_links"]["parent"] = {"href": f"/api/entities/{child_id}"}
    httpx_mock.add_response(json=grandchild)
    assert shell(["cd", ".."]) == 0
    assert state.current_entity_id == child_id
    assert shell(["cd", "/"]) == 0
    assert shell(["cd", "."]) == 0
    assert state.current_entity_id is None


@pytest.mark.parametrize(
    "path",
    [
        "",
        "child/",
        "child//grandchild",
        "10000000-0000-0000-0000-000000000001",
        "/api/entities/10000000-0000-0000-0000-000000000001",
        "https://app.example/entities/10000000-0000-0000-0000-000000000001",
    ],
)
def test_cd_rejects_invalid_path_forms_without_requesting_the_api(path: str, httpx_mock: HTTPXMock) -> None:
    shell = ShellDispatcher(lambda _: 0, CliState())

    with pytest.raises(InteractiveError):
        shell(["cd", path])

    assert httpx_mock.get_requests() == []


def test_pwd_reports_a_stale_location_with_root_recovery_guidance(httpx_mock: HTTPXMock) -> None:
    state = CliState(current_entity_id="10000000-0000-0000-0000-000000000001")
    shell = ShellDispatcher(lambda _: 0, state)
    httpx_mock.add_response(status_code=404, json={"detail": "Not Found"})

    with pytest.raises(StaleLocationError, match="run 'cd /'"):
        shell(["pwd"])

    assert state.current_entity_id == "10000000-0000-0000-0000-000000000001"


def test_use_resets_the_location_without_persisting_a_host() -> None:
    state = CliState(host="first.example", current_entity_id="entity-id")
    shell = ShellDispatcher(lambda _: 0, state)

    assert shell(["use", "other.example"]) == 0

    assert (state.host, state.current_entity_id) == ("other.example", None)


def test_use_closes_the_old_client_before_switching_hosts() -> None:
    state = CliState(host="first.example", current_entity_id="entity-id")
    old_client = state.session.client.get_httpx_client()
    shell = ShellDispatcher(lambda _: 0, state)

    shell(["use", "other.example"])

    assert old_client.is_closed
