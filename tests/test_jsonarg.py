"""Reading JSON payloads from --data and --field."""

from __future__ import annotations

import io
from pathlib import Path

import pytest

from jsonhub_cli import jsonarg
from jsonhub_cli.jsonarg import InputError


def test_no_source_is_distinguishable_from_an_empty_object() -> None:
    assert jsonarg.read_json(None, None) is None
    assert jsonarg.read_json("{}", None) == {}


def test_inline_json() -> None:
    assert jsonarg.read_json('{"name": "ada"}', None) == {"name": "ada"}


def test_file_source(tmp_path: Path) -> None:
    path = tmp_path / "doc.json"
    path.write_text('{"name": "ada"}')
    assert jsonarg.read_json(f"@{path}", None) == {"name": "ada"}


def test_stdin_source(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("sys.stdin", io.StringIO('{"name": "ada"}'))
    assert jsonarg.read_json("-", None) == {"name": "ada"}


def test_stdin_from_a_terminal_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    class Tty(io.StringIO):
        def isatty(self) -> bool:
            return True

    monkeypatch.setattr("sys.stdin", Tty(""))
    with pytest.raises(InputError, match="stdin is a terminal"):
        jsonarg.read_json("-", None)


def test_fields_are_typed_when_they_look_like_json() -> None:
    document = jsonarg.read_json(None, ["name=ada", "count=3", "ok=true", 'tags=["a","b"]', "nil=null"])
    assert document == {"name": "ada", "count": 3, "ok": True, "tags": ["a", "b"], "nil": None}


def test_dotted_field_names_nest() -> None:
    assert jsonarg.read_json(None, ["owner.name=ada", "owner.id=7"]) == {"owner": {"name": "ada", "id": 7}}


def test_dotted_field_replaces_a_scalar_on_the_path() -> None:
    assert jsonarg.read_json(None, ["owner=x", "owner.name=ada"]) == {"owner": {"name": "ada"}}


def test_fields_override_data() -> None:
    assert jsonarg.read_json('{"name": "old", "keep": 1}', ["name=new"]) == {"name": "new", "keep": 1}


def test_field_without_an_equals_sign_is_reported() -> None:
    with pytest.raises(InputError, match="expects 'name=value'"):
        jsonarg.read_json(None, ["justaname"])


def test_non_object_payload_is_refused() -> None:
    with pytest.raises(InputError, match="must be a JSON object"):
        jsonarg.read_json("[1, 2]", None)


def test_invalid_json_reports_the_position() -> None:
    with pytest.raises(InputError, match=r"line 1, column 2"):
        jsonarg.read_json("{oops}", None)


def test_missing_file_is_reported(tmp_path: Path) -> None:
    with pytest.raises(InputError, match="cannot read"):
        jsonarg.read_json(f"@{tmp_path / 'absent.json'}", None)


def test_require_json_explains_the_options() -> None:
    with pytest.raises(InputError) as excinfo:
        jsonarg.require_json(None, None, what="entity data")
    assert excinfo.value.hint is not None
    assert "--field" in excinfo.value.hint


def _fake_editor(tmp_path: Path, script: str) -> str:
    """An $EDITOR that runs ``script`` against the file it is handed."""
    path = tmp_path / "fake_editor.py"
    path.write_text(script)
    return f"python3 {path}"


def test_edit_json_round_trips_through_the_editor(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    editor = _fake_editor(tmp_path, "import sys, pathlib\npathlib.Path(sys.argv[1]).write_text('{\"v\": 2}')\n")
    monkeypatch.setenv("EDITOR", editor)
    monkeypatch.delenv("VISUAL", raising=False)
    assert jsonarg.edit_json({"v": 1}) == {"v": 2}


def test_visual_wins_over_editor(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EDITOR", _fake_editor(tmp_path, "raise SystemExit(9)\n"))
    monkeypatch.setenv(
        "VISUAL",
        _fake_editor(tmp_path, 'import sys, pathlib\npathlib.Path(sys.argv[1]).write_text(\'{"from": "visual"}\')\n'),
    )
    assert jsonarg.edit_json({}) == {"from": "visual"}


def test_editor_with_quoted_flags_is_split_like_a_shell(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    script = tmp_path / "flagged editor.py"
    script.write_text("import sys, pathlib\npathlib.Path(sys.argv[-1]).write_text('{\"ok\": true}')\n")
    monkeypatch.setenv("EDITOR", f'python3 "{script}"')
    monkeypatch.delenv("VISUAL", raising=False)
    assert jsonarg.edit_json({}) == {"ok": True}


def test_edit_json_reports_an_editor_that_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EDITOR", _fake_editor(tmp_path, "raise SystemExit(3)\n"))
    monkeypatch.delenv("VISUAL", raising=False)
    with pytest.raises(InputError, match="exited with status 3"):
        jsonarg.edit_json({"v": 1})


def test_edit_json_refuses_an_emptied_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    editor = _fake_editor(tmp_path, "import sys, pathlib\npathlib.Path(sys.argv[1]).write_text('')\n")
    monkeypatch.setenv("EDITOR", editor)
    monkeypatch.delenv("VISUAL", raising=False)
    with pytest.raises(InputError, match="left empty"):
        jsonarg.edit_json({"v": 1})


def test_missing_editor_binary_is_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EDITOR", "definitely-not-an-editor-9fj3")
    monkeypatch.delenv("VISUAL", raising=False)
    with pytest.raises(InputError, match="could not launch editor"):
        jsonarg.edit_json({"v": 1})
