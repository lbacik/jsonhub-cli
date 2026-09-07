"""End-to-end tests for ``jsonhub entity``, against a mocked API."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pytest_httpx import HTTPXMock

from .conftest import TOKEN, entity, hal_collection

ENTITY_ID = "018baea0-f999-73f4-9eb4-d0c62f3ac49b"
DEFINITION_ID = "d0000000-0000-0000-0000-000000000001"


def test_list_prints_a_row_per_entity(httpx_mock: HTTPXMock, invoke: Any) -> None:
    httpx_mock.add_response(json=hal_collection(entity(), entity("other-id", "second"), total=2))

    result = invoke("entity", "list")

    assert result.exit_code == 0
    assert "testing" in result.stdout
    assert "second" in result.stdout


def test_list_sends_hal_accept_so_the_envelope_comes_back(httpx_mock: HTTPXMock, invoke: Any) -> None:
    httpx_mock.add_response(json=hal_collection(entity()))

    invoke("entity", "list")

    assert httpx_mock.get_requests()[0].headers["Accept"] == "application/hal+json"


def test_list_json_prints_only_the_items_array(httpx_mock: HTTPXMock, invoke: Any) -> None:
    httpx_mock.add_response(json=hal_collection(entity(), total=1))

    result = invoke("entity", "list", "--json")

    payload = json.loads(result.stdout)
    assert [item["slug"] for item in payload] == ["testing"]


def test_list_survives_an_empty_collection(httpx_mock: HTTPXMock, invoke: Any) -> None:
    # JsonHub omits _embedded when nothing matches, which the generated SDK
    # model does not tolerate; the CLI must still exit cleanly.
    httpx_mock.add_response(json={"_links": {"self": {"href": "/api/entities"}}, "totalItems": 0, "itemsPerPage": 10})

    result = invoke("entity", "list", "--search", "nothing-matches")

    assert result.exit_code == 0
    assert "No entities found" in result.stderr


def test_list_empty_json_is_an_empty_array(httpx_mock: HTTPXMock, invoke: Any) -> None:
    httpx_mock.add_response(json={"_links": {}, "totalItems": 0})

    result = invoke("entity", "list", "--json")

    assert json.loads(result.stdout) == []


def test_list_passes_filters_through_as_query_params(httpx_mock: HTTPXMock, invoke: Any) -> None:
    httpx_mock.add_response(json=hal_collection(definition_lookup := definition_body()))
    httpx_mock.add_response(json=hal_collection(entity()))

    invoke("entity", "list", "--definition", "base-v1", "--owned", "--limit", "5")

    assert definition_lookup["slug"] == "base-v1"
    params = httpx_mock.get_requests()[-1].url.params
    assert params["definition"] == DEFINITION_ID
    assert params["owned"] == "true"
    assert params["limit"] == "5"


def test_get_by_slug_resolves_then_fetches(httpx_mock: HTTPXMock, invoke: Any) -> None:
    httpx_mock.add_response(json=hal_collection(entity()))
    httpx_mock.add_response(json=entity())

    result = invoke("entity", "get", "testing")

    assert result.exit_code == 0
    lookup, fetch = httpx_mock.get_requests()
    assert lookup.url.params["qid"] == "testing"
    assert fetch.url.path == f"/api/entities/{ENTITY_ID}"


def test_get_by_uuid_skips_the_slug_lookup(httpx_mock: HTTPXMock, invoke: Any) -> None:
    httpx_mock.add_response(json=entity())

    invoke("entity", "get", ENTITY_ID)

    assert len(httpx_mock.get_requests()) == 1


def test_get_data_only_prints_just_the_document(httpx_mock: HTTPXMock, invoke: Any) -> None:
    httpx_mock.add_response(json=entity(data={"name": "ada", "n": 2}))

    result = invoke("entity", "get", ENTITY_ID, "--data-only")

    assert json.loads(result.stdout) == {"name": "ada", "n": 2}


def test_slug_needs_an_exact_match(httpx_mock: HTTPXMock, invoke: Any) -> None:
    # qid matches substrings, so a partial hit must not be accepted silently.
    httpx_mock.add_response(json=hal_collection(entity(slug="testing-other")))

    result = invoke("entity", "get", "testing")

    assert result.exit_code == 3
    assert "no entity with slug 'testing'" in result.stderr


def test_ambiguous_slug_refuses_to_guess(httpx_mock: HTTPXMock, invoke: Any) -> None:
    httpx_mock.add_response(json=hal_collection(entity("id-one", "dup"), entity("id-two", "dup")))

    result = invoke("entity", "get", "dup")

    assert result.exit_code == 2
    assert "matches 2 entitys" in result.stderr or "matches 2" in result.stderr


def test_create_requires_credentials(httpx_mock: HTTPXMock, invoke: Any) -> None:
    result = invoke("entity", "create", "--field", "name=ada")

    assert result.exit_code == 4
    assert "not logged in" in result.stderr
    assert httpx_mock.get_requests() == []


def test_create_posts_the_document_and_bearer_token(httpx_mock: HTTPXMock, invoke: Any, logged_in: Path) -> None:
    httpx_mock.add_response(json=entity(), status_code=201)

    result = invoke("entity", "create", "--field", "name=ada", "--field", "count=3")

    assert result.exit_code == 0
    request = httpx_mock.get_requests()[0]
    assert request.method == "POST"
    assert request.headers["Authorization"] == f"Bearer {TOKEN}"
    assert json.loads(request.content)["data"] == {"name": "ada", "count": 3}


def test_create_resolves_a_definition_slug_to_an_iri(httpx_mock: HTTPXMock, invoke: Any, logged_in: Path) -> None:
    httpx_mock.add_response(json=hal_collection(definition_body()))
    httpx_mock.add_response(json=entity(), status_code=201)

    invoke("entity", "create", "-d", '{"name": "ada"}', "--definition", "base-v1")

    body = json.loads(httpx_mock.get_requests()[-1].content)
    assert body["definition"] == f"/api/definitions/{DEFINITION_ID}"


def test_create_marks_private_entities(httpx_mock: HTTPXMock, invoke: Any, logged_in: Path) -> None:
    httpx_mock.add_response(json=entity(private=True), status_code=201)

    invoke("entity", "create", "-d", "{}", "--private", "--slug", "secret")

    body = json.loads(httpx_mock.get_requests()[0].content)
    assert (body["private"], body["slug"]) == (True, "secret")


def test_create_reports_validation_violations(httpx_mock: HTTPXMock, invoke: Any, logged_in: Path) -> None:
    httpx_mock.add_response(
        status_code=422,
        json={
            "status": 422,
            "detail": "data.name: This value is too short.",
            "violations": [{"propertyPath": "data.name", "message": "This value is too short."}],
        },
    )

    result = invoke("entity", "create", "-d", '{"name": "a"}')

    assert result.exit_code == 22
    assert "too short" in result.stderr
    assert "data.name" in result.stderr


def test_edit_sends_a_merge_patch(httpx_mock: HTTPXMock, invoke: Any, logged_in: Path) -> None:
    httpx_mock.add_response(json=entity())

    invoke("entity", "edit", ENTITY_ID, "--field", "name=updated")

    request = httpx_mock.get_requests()[0]
    assert request.method == "PATCH"
    assert request.headers["Content-Type"] == "application/merge-patch+json"
    assert json.loads(request.content)["data"] == {"name": "updated"}


def test_edit_can_change_visibility_without_touching_data(httpx_mock: HTTPXMock, invoke: Any, logged_in: Path) -> None:
    httpx_mock.add_response(json=entity())

    invoke("entity", "edit", ENTITY_ID, "--public")

    body = json.loads(httpx_mock.get_requests()[0].content)
    assert body == {"private": False}


def test_edit_does_not_publish_a_private_entity_by_accident(
    httpx_mock: HTTPXMock, invoke: Any, logged_in: Path
) -> None:
    # The generated merge-patch model defaults private to False rather than
    # UNSET, so an edit that never mentions visibility must not send it.
    httpx_mock.add_response(json=entity())

    invoke("entity", "edit", ENTITY_ID, "--slug", "renamed")

    assert json.loads(httpx_mock.get_requests()[0].content) == {"slug": "renamed"}


def test_edit_can_make_an_entity_private(httpx_mock: HTTPXMock, invoke: Any, logged_in: Path) -> None:
    httpx_mock.add_response(json=entity(private=True))

    invoke("entity", "edit", ENTITY_ID, "--private")

    assert json.loads(httpx_mock.get_requests()[0].content) == {"private": True}


def test_delete_asks_before_destroying(httpx_mock: HTTPXMock, invoke: Any, logged_in: Path) -> None:
    result = invoke("entity", "delete", ENTITY_ID, stdin="n\n")

    assert result.exit_code != 0
    assert httpx_mock.get_requests() == []


def test_delete_proceeds_when_confirmed(httpx_mock: HTTPXMock, invoke: Any, logged_in: Path) -> None:
    httpx_mock.add_response(status_code=204)

    result = invoke("entity", "delete", ENTITY_ID, stdin="y\n")

    assert result.exit_code == 0
    assert httpx_mock.get_requests()[0].method == "DELETE"


def test_delete_with_yes_skips_the_prompt(httpx_mock: HTTPXMock, invoke: Any, logged_in: Path) -> None:
    httpx_mock.add_response(status_code=204)

    result = invoke("entity", "delete", ENTITY_ID, "--yes")

    assert result.exit_code == 0
    assert httpx_mock.get_requests()[0].method == "DELETE"


def test_a_404_is_reported_without_a_traceback(httpx_mock: HTTPXMock, invoke: Any) -> None:
    httpx_mock.add_response(status_code=404, json={"title": "An error occurred", "detail": "Not Found", "status": 404})

    result = invoke("entity", "get", ENTITY_ID)

    assert result.exit_code == 3
    assert "Not Found" in result.stderr
    assert "Traceback" not in result.stderr


def test_a_401_points_the_user_at_login(httpx_mock: HTTPXMock, invoke: Any, logged_in: Path) -> None:
    httpx_mock.add_response(status_code=401, json={"detail": "Invalid credentials"})

    result = invoke("entity", "get", ENTITY_ID)

    assert result.exit_code == 4
    assert "auth login" in result.stderr


def definition_body() -> dict[str, Any]:
    return {
        "_links": {"self": {"href": f"/api/definitions/{DEFINITION_ID}"}},
        "id": DEFINITION_ID,
        "slug": "base-v1",
        "jsonSchema": {"type": "object"},
    }
