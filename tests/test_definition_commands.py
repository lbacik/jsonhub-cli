"""End-to-end tests for ``jsonhub definition``."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pytest_httpx import HTTPXMock

from jsonhub_cli.commands._shared import CliState
from jsonhub_cli.main import run

from .conftest import definition, hal_collection

DEFINITION_ID = "d0000000-0000-0000-0000-000000000001"
ENTITY_ID = "018baea0-f999-73f4-9eb4-d0c62f3ac49b"
SCHEMA = {"title": "Person", "type": "object", "properties": {"name": {"type": "string"}}}


def test_list_shows_slug_and_schema_properties(httpx_mock: HTTPXMock, invoke: Any) -> None:
    httpx_mock.add_response(json=hal_collection(definition(schema=SCHEMA), total=1))

    result = invoke("definition", "list")

    assert result.exit_code == 0
    assert "base-v1" in result.stdout
    assert "Person" in result.stdout
    assert "name" in result.stdout


def test_list_survives_an_empty_collection(httpx_mock: HTTPXMock, invoke: Any) -> None:
    httpx_mock.add_response(json={"_links": {}, "totalItems": 0, "itemsPerPage": 10})

    result = invoke("definition", "list", "--search", "nothing")

    assert result.exit_code == 0
    assert "No definitions found" in result.stderr


def test_list_asks_for_root_definitions_only(httpx_mock: HTTPXMock, invoke: Any) -> None:
    httpx_mock.add_response(json=hal_collection(definition()))

    invoke("definition", "list", "--root")

    assert httpx_mock.get_requests()[0].url.params["root"] == "true"


def test_list_asks_for_nested_definitions_only(httpx_mock: HTTPXMock, invoke: Any) -> None:
    httpx_mock.add_response(json=hal_collection(definition()))

    invoke("definition", "list", "--nested")

    assert httpx_mock.get_requests()[0].url.params["root"] == "false"


def test_list_leaves_root_out_when_it_was_not_asked_for(httpx_mock: HTTPXMock, invoke: Any) -> None:
    httpx_mock.add_response(json=hal_collection(definition()))

    invoke("definition", "list")

    assert "root" not in httpx_mock.get_requests()[0].url.params


def test_list_uses_the_interactive_location_as_its_default_parent_entity(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(json=hal_collection(definition()))

    assert run(["definition", "list"], state=CliState(current_entity_id=ENTITY_ID)) == 0

    assert httpx_mock.get_requests()[0].url.params["parentEntity"] == ENTITY_ID


def test_list_root_bypasses_the_interactive_location(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(json=hal_collection(definition()))

    assert run(["definition", "list", "--root"], state=CliState(current_entity_id=ENTITY_ID)) == 0

    params = httpx_mock.get_requests()[0].url.params
    assert params["root"] == "true"
    assert "parentEntity" not in params


def test_list_nested_bypasses_the_interactive_location(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(json=hal_collection(definition()))

    assert run(["definition", "list", "--nested"], state=CliState(current_entity_id=ENTITY_ID)) == 0

    params = httpx_mock.get_requests()[0].url.params
    assert params["root"] == "false"
    assert "parentEntity" not in params


def test_list_explicit_parent_entity_wins_over_the_interactive_location(httpx_mock: HTTPXMock) -> None:
    explicit_parent = "10000000-0000-0000-0000-000000000004"
    httpx_mock.add_response(json=hal_collection(definition()))

    assert (
        run(
            ["definition", "list", "--parent-entity", explicit_parent],
            state=CliState(current_entity_id=ENTITY_ID),
        )
        == 0
    )

    assert httpx_mock.get_requests()[0].url.params["parentEntity"] == explicit_parent


def test_get_schema_only_prints_just_the_schema(httpx_mock: HTTPXMock, invoke: Any) -> None:
    httpx_mock.add_response(json=definition(schema=SCHEMA))

    result = invoke("definition", "get", DEFINITION_ID, "--schema-only")

    assert json.loads(result.stdout) == SCHEMA


def test_create_posts_the_schema_under_jsonschema(httpx_mock: HTTPXMock, invoke: Any, logged_in: Path) -> None:
    httpx_mock.add_response(json=definition(), status_code=201)

    result = invoke("definition", "create", "-d", json.dumps(SCHEMA), "--slug", "person-v1")

    assert result.exit_code == 0
    body = json.loads(httpx_mock.get_requests()[0].content)
    assert body["jsonSchema"] == SCHEMA
    assert body["slug"] == "person-v1"


def test_create_from_a_file(httpx_mock: HTTPXMock, invoke: Any, logged_in: Path, tmp_path: Path) -> None:
    path = tmp_path / "schema.json"
    path.write_text(json.dumps(SCHEMA))
    httpx_mock.add_response(json=definition(), status_code=201)

    invoke("definition", "create", "-d", f"@{path}")

    assert json.loads(httpx_mock.get_requests()[0].content)["jsonSchema"] == SCHEMA


def test_create_from_stdin(httpx_mock: HTTPXMock, invoke: Any, logged_in: Path) -> None:
    httpx_mock.add_response(json=definition(), status_code=201)

    invoke("definition", "create", "-d", "-", stdin=json.dumps(SCHEMA))

    assert json.loads(httpx_mock.get_requests()[0].content)["jsonSchema"] == SCHEMA


def test_create_resolves_a_parent_entity_slug(httpx_mock: HTTPXMock, invoke: Any, logged_in: Path) -> None:
    httpx_mock.add_response(json=hal_collection({"id": ENTITY_ID, "slug": "root"}))
    httpx_mock.add_response(json=definition(), status_code=201)

    invoke("definition", "create", "-d", "{}", "--parent-entity", "root")

    body = json.loads(httpx_mock.get_requests()[-1].content)
    assert body["parentEntity"] == f"/api/entities/{ENTITY_ID}"


def test_create_uses_the_interactive_location_as_its_default_parent_entity(
    httpx_mock: HTTPXMock, logged_in: Path
) -> None:
    httpx_mock.add_response(json=definition(), status_code=201)

    assert run(["definition", "create", "-d", "{}"], state=CliState(current_entity_id=ENTITY_ID)) == 0

    assert json.loads(httpx_mock.get_requests()[0].content)["parentEntity"] == f"/api/entities/{ENTITY_ID}"


def test_create_root_bypasses_the_interactive_location(httpx_mock: HTTPXMock, logged_in: Path) -> None:
    httpx_mock.add_response(json=definition(), status_code=201)

    assert run(["definition", "create", "-d", "{}", "--root"], state=CliState(current_entity_id=ENTITY_ID)) == 0

    assert "parentEntity" not in json.loads(httpx_mock.get_requests()[0].content)


def test_create_explicit_parent_entity_wins_over_the_interactive_location(
    httpx_mock: HTTPXMock, logged_in: Path
) -> None:
    explicit_parent = "10000000-0000-0000-0000-000000000004"
    httpx_mock.add_response(json=definition(), status_code=201)

    assert (
        run(
            ["definition", "create", "-d", "{}", "--parent-entity", explicit_parent],
            state=CliState(current_entity_id=ENTITY_ID),
        )
        == 0
    )

    assert json.loads(httpx_mock.get_requests()[0].content)["parentEntity"] == f"/api/entities/{explicit_parent}"


def test_edit_sends_a_merge_patch_of_the_schema(httpx_mock: HTTPXMock, invoke: Any, logged_in: Path) -> None:
    httpx_mock.add_response(json=definition())

    invoke("definition", "edit", DEFINITION_ID, "-d", json.dumps(SCHEMA))

    request = httpx_mock.get_requests()[0]
    assert request.headers["Content-Type"] == "application/merge-patch+json"
    assert json.loads(request.content)["jsonSchema"] == SCHEMA


def test_edit_opens_the_editor_when_given_nothing(
    httpx_mock: HTTPXMock, invoke: Any, logged_in: Path, tmp_path: Any, monkeypatch: Any
) -> None:
    script = tmp_path / "editor.py"
    script.write_text("import sys, pathlib\npathlib.Path(sys.argv[1]).write_text('{\"edited\": true}')\n")
    monkeypatch.setenv("EDITOR", f"python3 {script}")
    monkeypatch.delenv("VISUAL", raising=False)

    httpx_mock.add_response(json=definition(schema=SCHEMA))  # the fetch that seeds the editor
    httpx_mock.add_response(json=definition())  # the patch

    result = invoke("definition", "edit", DEFINITION_ID)

    assert result.exit_code == 0, result.stderr
    patch = httpx_mock.get_requests()[-1]
    assert patch.method == "PATCH"
    assert json.loads(patch.content)["jsonSchema"] == {"edited": True}


def test_rename_only_sends_the_slug(httpx_mock: HTTPXMock, invoke: Any, logged_in: Path) -> None:
    httpx_mock.add_response(json=definition())

    invoke("definition", "edit", DEFINITION_ID, "--slug", "renamed-v2")

    assert json.loads(httpx_mock.get_requests()[0].content) == {"slug": "renamed-v2"}


def test_delete_requires_confirmation(httpx_mock: HTTPXMock, invoke: Any, logged_in: Path) -> None:
    result = invoke("definition", "delete", DEFINITION_ID, stdin="n\n")

    assert result.exit_code != 0
    assert httpx_mock.get_requests() == []
