"""End-to-end tests for ``jsonhub token`` and ``jsonhub me``."""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from typing import Any

from pytest_httpx import HTTPXMock

from .conftest import hal_collection, quota

TOKEN_ID = "7f000000-0000-0000-0000-00000000000a"


def stored_token(token_id: str = TOKEN_ID, name: str = "ci-deploy") -> dict[str, Any]:
    return {
        "_links": {"self": {"href": f"/api/me/api-tokens/{token_id}"}},
        "id": token_id,
        "name": name,
        "tokenPreview": "jh_abc...",
        "createdAt": "2026-01-15T09:30:00+00:00",
        "expiresAt": "2026-04-15T09:30:00+00:00",
    }


def test_list_requires_credentials(httpx_mock: HTTPXMock, invoke: Any) -> None:
    result = invoke("token", "list")

    assert result.exit_code == 4
    assert httpx_mock.get_requests() == []


def test_list_renders_dates_as_plain_days(httpx_mock: HTTPXMock, invoke: Any, logged_in: Path) -> None:
    httpx_mock.add_response(json=hal_collection(stored_token(), total=1))

    result = invoke("token", "list")

    assert result.exit_code == 0
    assert "ci-deploy" in result.stdout
    assert "2026-01-15" in result.stdout
    assert "2026-04-15" in result.stdout


def test_list_says_never_when_there_is_no_expiry(httpx_mock: HTTPXMock, invoke: Any, logged_in: Path) -> None:
    token = stored_token()
    del token["expiresAt"]
    httpx_mock.add_response(json=hal_collection(token, total=1))

    result = invoke("token", "list")

    assert "never" in result.stdout


def test_list_survives_an_account_with_no_tokens(httpx_mock: HTTPXMock, invoke: Any, logged_in: Path) -> None:
    httpx_mock.add_response(json={"_links": {}, "totalItems": 0})

    result = invoke("token", "list")

    assert result.exit_code == 0
    assert "No personal access tokens" in result.stderr


def test_create_prints_the_secret_exactly_once(httpx_mock: HTTPXMock, invoke: Any, logged_in: Path) -> None:
    created = stored_token() | {"token": "jh_the_real_secret"}
    httpx_mock.add_response(json=created, status_code=201)

    result = invoke("token", "create", "ci-deploy")

    assert result.exit_code == 0
    assert result.stdout.strip() == "jh_the_real_secret"
    assert "only time" in result.stderr


def test_create_warns_if_the_server_withholds_the_secret(httpx_mock: HTTPXMock, invoke: Any, logged_in: Path) -> None:
    httpx_mock.add_response(json=stored_token(), status_code=201)

    result = invoke("token", "create", "ci-deploy")

    assert "did not return the token secret" in result.stderr


def test_create_accepts_a_day_count_as_the_expiry(httpx_mock: HTTPXMock, invoke: Any, logged_in: Path) -> None:
    httpx_mock.add_response(json=stored_token(), status_code=201)

    invoke("token", "create", "ci", "--expires", "90d")

    sent = json.loads(httpx_mock.get_requests()[0].content)["expiresAt"]
    expected = dt.datetime.now(dt.UTC) + dt.timedelta(days=90)
    assert abs((dt.datetime.fromisoformat(sent) - expected).total_seconds()) < 60


def test_create_accepts_an_iso_date_as_the_expiry(httpx_mock: HTTPXMock, invoke: Any, logged_in: Path) -> None:
    httpx_mock.add_response(json=stored_token(), status_code=201)

    invoke("token", "create", "ci", "--expires", "2027-01-31")

    sent = json.loads(httpx_mock.get_requests()[0].content)["expiresAt"]
    assert dt.datetime.fromisoformat(sent).date().isoformat() == "2027-01-31"


def test_create_rejects_an_unreadable_expiry(httpx_mock: HTTPXMock, invoke: Any, logged_in: Path) -> None:
    result = invoke("token", "create", "ci", "--expires", "next tuesday")

    assert result.exit_code == 1
    assert "could not read" in result.stderr
    assert httpx_mock.get_requests() == []


def test_tokens_are_addressed_by_id_not_name(httpx_mock: HTTPXMock, invoke: Any, logged_in: Path) -> None:
    result = invoke("token", "delete", "ci-deploy", "--yes")

    assert result.exit_code == 1
    assert "is not a token id" in result.stderr
    assert httpx_mock.get_requests() == []


def test_delete_revokes_after_confirmation(httpx_mock: HTTPXMock, invoke: Any, logged_in: Path) -> None:
    httpx_mock.add_response(status_code=204)

    result = invoke("token", "delete", TOKEN_ID, stdin="y\n")

    assert result.exit_code == 0
    assert httpx_mock.get_requests()[0].method == "DELETE"


def test_rename_patches_the_name(httpx_mock: HTTPXMock, invoke: Any, logged_in: Path) -> None:
    httpx_mock.add_response(json=stored_token(name="new-name"))

    invoke("token", "rename", TOKEN_ID, "new-name")

    request = httpx_mock.get_requests()[0]
    assert request.headers["Content-Type"] == "application/merge-patch+json"
    assert json.loads(request.content) == {"name": "new-name"}


def test_me_shows_usage_against_limits(httpx_mock: HTTPXMock, invoke: Any, logged_in: Path) -> None:
    httpx_mock.add_response(json=quota(entities=7, private=2, definitions=3))

    result = invoke("me")

    assert result.exit_code == 0
    assert "entities" in result.stdout
    assert "93" in result.stdout  # 100 - 7 remaining


def test_me_json_is_the_api_body(httpx_mock: HTTPXMock, invoke: Any, logged_in: Path) -> None:
    httpx_mock.add_response(json=quota())

    result = invoke("me", "--json")

    assert json.loads(result.stdout)["limits"]["entities"]["limit"] == 100


def test_me_requires_credentials(httpx_mock: HTTPXMock, invoke: Any) -> None:
    result = invoke("me")

    assert result.exit_code == 4
    assert httpx_mock.get_requests() == []
