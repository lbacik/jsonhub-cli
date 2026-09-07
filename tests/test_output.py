"""Output shaping: machine-readable when piped, tables when interactive."""

from __future__ import annotations

from typing import Any

import pytest

from jsonhub_cli import output, refs


def test_piped_tables_are_tab_separated_without_a_header(capsys: pytest.CaptureFixture[str]) -> None:
    output.print_table(["A", "B"], [["one", "two"]])

    captured = capsys.readouterr()
    assert captured.out == "one\ttwo\n"


def test_piped_cells_never_contain_a_stray_tab_or_newline(capsys: pytest.CaptureFixture[str]) -> None:
    output.print_table(["A"], [["has\ttab and\nnewline"]])

    assert capsys.readouterr().out == "has tab and newline\n"


def test_booleans_are_lowercase_json_when_piped(capsys: pytest.CaptureFixture[str]) -> None:
    output.print_table(["A", "B"], [[True, False]])

    assert capsys.readouterr().out == "true\tfalse\n"


def test_none_becomes_an_empty_column_when_piped(capsys: pytest.CaptureFixture[str]) -> None:
    output.print_table(["A", "B"], [[None, "x"]])

    assert capsys.readouterr().out == "\tx\n"


def test_an_empty_result_says_so_on_stderr(capsys: pytest.CaptureFixture[str]) -> None:
    output.print_table(["A"], [], empty="Nothing here")

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "Nothing here" in captured.err


def test_json_output_goes_to_stdout_alone(capsys: pytest.CaptureFixture[str]) -> None:
    output.note("a diagnostic")
    output.print_json({"a": 1})

    captured = capsys.readouterr()
    assert captured.out.strip() == '{\n  "a": 1\n}'
    assert "a diagnostic" in captured.err


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ({"a": 1}, '{"a":1}'),
        ([1, 2], "[1,2]"),
        (None, ""),
        ("plain", "plain"),
    ],
)
def test_summarize_json_is_compact(value: Any, expected: str) -> None:
    assert output.summarize_json(value) == expected


def test_summarize_json_truncates_with_an_ellipsis() -> None:
    summary = output.summarize_json({"key": "v" * 200}, width=20)

    assert len(summary) == 20
    assert summary.endswith("...")


def test_truncate_leaves_short_values_alone() -> None:
    assert output.truncate("short", 20) == "short"


def test_id_is_read_from_the_self_link_when_absent() -> None:
    resource = {"_links": {"self": {"href": "/api/entities/abc-123"}}}

    assert refs.resource_id(resource) == "abc-123"


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("/api/entities/abc", "abc"),
        ("https://api.example/api/entities/abc", "abc"),
        ("https://app.example/entities/abc/", "abc"),
        ("bare", None),
    ],
)
def test_id_from_iri_handles_paths_and_urls(value: str, expected: str | None) -> None:
    assert refs.id_from_iri(value) == expected
