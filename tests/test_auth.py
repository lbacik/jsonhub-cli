"""Login, logout and credential inspection.

The OAuth flow is driven end to end: the API is mocked with ``pytest_httpx``,
while the browser step is stood in for by a thread that performs the redirect
against the CLI's real loopback listener over ``urllib`` (which the httpx mock
does not intercept).
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import json
import threading
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from pytest_httpx import HTTPXMock

from jsonhub_cli import oauth
from jsonhub_cli.config import Config

from .conftest import BASE_URL, HOST, TOKEN, quota

# The happy-path fixture registers every endpoint a full login touches; tests
# that stop early legitimately leave some unused.
pytestmark = pytest.mark.httpx_mock(assert_all_responses_were_requested=False)

METADATA = {
    "issuer": BASE_URL,
    "authorization_endpoint": f"{BASE_URL}/oauth2/authorize",
    "token_endpoint": f"{BASE_URL}/oauth2/token",
    "registration_endpoint": f"{BASE_URL}/oauth2/register",
    "revocation_endpoint": f"{BASE_URL}/oauth2/revoke",
    "response_types_supported": ["code"],
    "grant_types_supported": ["authorization_code"],
    "code_challenge_methods_supported": ["S256"],
    "scopes_supported": [
        "mcp",
        "frontend",
        "jsonhub:entities:read",
        "jsonhub:entities:write",
        "jsonhub:definitions:write",
    ],
    "audiences_supported": ["mcp", "jsonhub-api"],
}


@pytest.fixture
def oauth_server(httpx_mock: HTTPXMock) -> None:
    """Mock the four endpoints a successful browser login touches."""
    _mock_oauth_discovery_and_registration(httpx_mock)
    httpx_mock.add_response(
        url=f"{BASE_URL}/oauth2/token",
        json={
            "access_token": "oauth-access-token",
            "token_type": "Bearer",
            "expires_in": 3600,
            "scope": "jsonhub:entities:read jsonhub:entities:write jsonhub:definitions:write",
        },
    )
    httpx_mock.add_response(url=f"{BASE_URL}/api/users/me", json=quota())


def _mock_oauth_discovery_and_registration(httpx_mock: HTTPXMock) -> None:
    """Mock the common bootstrap shared by browser-flow scenarios."""
    httpx_mock.add_response(url=f"{BASE_URL}/.well-known/oauth-authorization-server", json=METADATA)
    httpx_mock.add_response(url=f"{BASE_URL}/oauth2/register", status_code=201, json={"client_id": "cli-client-1"})


def _write_host_config(config_dir: Path, **values: Any) -> None:
    """Store one host entry for credential transition and logout scenarios."""
    config_dir.mkdir(parents=True, exist_ok=True)
    entry = {"base_url": BASE_URL, **values}
    (config_dir / "config.json").write_text(json.dumps({"default_host": HOST, "hosts": {HOST: entry}}))


def _redirect_in_background(callback_url: str) -> None:
    """Visit the CLI's loopback callback from another thread.

    The CLI only starts serving after ``webbrowser.open`` returns, so a
    synchronous fetch here would deadlock. urllib is used deliberately:
    ``pytest_httpx`` mocks httpx, and this request must really be made.
    """

    def visit() -> None:
        with contextlib.suppress(urllib.error.URLError):
            urllib.request.urlopen(callback_url, timeout=10).read()

    threading.Thread(target=visit, daemon=True).start()


def _browser_that_redirects_to(query: str, monkeypatch: pytest.MonkeyPatch) -> list[dict[str, str]]:
    """Stand in for the browser, appending ``query`` to the redirect URI."""
    seen: list[dict[str, str]] = []

    def _open(url: str, *args: Any, **kwargs: Any) -> bool:
        params = {k: v[0] for k, v in parse_qs(urlsplit(url).query).items()}
        seen.append(params)
        _redirect_in_background(f"{params['redirect_uri']}?{query.format(**params)}")
        return True

    monkeypatch.setattr("webbrowser.open", _open)
    return seen


@pytest.fixture
def fake_browser(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, str]]:
    """A browser that approves the request and returns a valid code."""
    return _browser_that_redirects_to("code=the-auth-code&state={state}", monkeypatch)


def test_browser_login_stores_the_access_token(
    invoke: Any, oauth_server: None, fake_browser: list[dict[str, str]], isolated_env: Path, transport_settings: Any
) -> None:
    result = invoke("--host", HOST, "--insecure", "auth", "login", "--base-url", BASE_URL)

    assert result.exit_code == 0, result.stderr
    stored = json.loads((isolated_env / "config.json").read_text())["hosts"][HOST]
    assert stored["token"] == "oauth-access-token"
    assert stored["token_type"] == "oauth"
    assert stored["client_id"] == "cli-client-1"
    assert stored["audience"] == "jsonhub-api"
    assert stored["insecure"] is True
    assert transport_settings and all(settings["verify_ssl"] is False for settings in transport_settings)


def test_browser_login_uses_pkce_with_s256(invoke: Any, oauth_server: None, fake_browser: list[dict[str, str]]) -> None:
    invoke("--host", HOST, "auth", "login", "--base-url", BASE_URL)

    authorize = fake_browser[0]
    assert authorize["code_challenge_method"] == "S256"
    assert authorize["response_type"] == "code"
    assert authorize["redirect_uri"].startswith("http://127.0.0.1:")
    assert len(authorize["code_challenge"]) >= 43


def test_the_verifier_matches_the_challenge_it_was_sent_with(
    httpx_mock: HTTPXMock, invoke: Any, oauth_server: None, fake_browser: list[dict[str, str]]
) -> None:
    invoke("--host", HOST, "auth", "login", "--base-url", BASE_URL)

    token_request = next(r for r in httpx_mock.get_requests() if r.url.path == "/oauth2/token")
    # The token endpoint takes form-encoded parameters, as OAuth 2.0 requires.
    body = {k: v[0] for k, v in parse_qs(token_request.content.decode()).items()}
    verifier = body["code_verifier"]
    digest = hashlib.sha256(verifier.encode()).digest()
    assert base64.urlsafe_b64encode(digest).decode().rstrip("=") == fake_browser[0]["code_challenge"]


def test_browser_login_requests_an_api_audience_and_cli_capabilities(
    httpx_mock: HTTPXMock, invoke: Any, oauth_server: None, fake_browser: list[dict[str, str]]
) -> None:
    invoke("--host", HOST, "auth", "login", "--base-url", BASE_URL)

    expected_scope = "jsonhub:entities:read jsonhub:entities:write jsonhub:definitions:write"
    assert fake_browser[0]["resource"] == "jsonhub-api"
    assert fake_browser[0]["scope"] == expected_scope
    registration = next(r for r in httpx_mock.get_requests() if r.url.path == "/oauth2/register")
    assert json.loads(registration.content)["scope"] == expected_scope


def test_browser_login_fails_when_the_api_audience_is_not_advertised(
    httpx_mock: HTTPXMock, invoke: Any, fake_browser: list[dict[str, str]]
) -> None:
    metadata = {**METADATA, "audiences_supported": ["mcp"]}
    httpx_mock.add_response(url=f"{BASE_URL}/.well-known/oauth-authorization-server", json=metadata)

    result = invoke("--host", HOST, "auth", "login", "--base-url", BASE_URL)

    assert result.exit_code == 4
    assert "jsonhub-api" in result.stderr
    assert "personal access token" in result.stderr
    assert fake_browser == []


def test_browser_login_is_verified_by_an_audience_aware_api(
    httpx_mock: HTTPXMock, invoke: Any, fake_browser: list[dict[str, str]], isolated_env: Path
) -> None:
    _mock_oauth_discovery_and_registration(httpx_mock)

    def issue_audience_bound_token(_request: httpx.Request) -> httpx.Response:
        audience = fake_browser[0].get("resource") if fake_browser else None
        token = "api-audience-token" if audience == "jsonhub-api" else "wrong-audience-token"
        return httpx.Response(200, json={"access_token": token, "token_type": "Bearer", "expires_in": 900})

    httpx_mock.add_callback(issue_audience_bound_token, url=f"{BASE_URL}/oauth2/token")

    def verify_audience(request: httpx.Request) -> httpx.Response:
        status = 200 if request.headers.get("Authorization") == "Bearer api-audience-token" else 401
        return httpx.Response(status, json=quota() if status == 200 else {"detail": "Invalid token audience"})

    httpx_mock.add_callback(verify_audience, url=f"{BASE_URL}/api/users/me")

    result = invoke("--host", HOST, "auth", "login", "--base-url", BASE_URL)

    assert result.exit_code == 0, result.stderr
    stored = json.loads((isolated_env / "config.json").read_text())["hosts"][HOST]
    assert stored["token"] == "api-audience-token"


def test_browser_login_rejects_a_token_missing_requested_scopes(
    httpx_mock: HTTPXMock, invoke: Any, fake_browser: list[dict[str, str]], isolated_env: Path
) -> None:
    _mock_oauth_discovery_and_registration(httpx_mock)
    httpx_mock.add_response(
        url=f"{BASE_URL}/oauth2/token",
        json={"access_token": "downscoped-token", "token_type": "Bearer", "scope": "jsonhub:entities:read"},
    )

    result = invoke("--host", HOST, "auth", "login", "--base-url", BASE_URL)

    assert result.exit_code == 4
    assert "requested OAuth scopes" in result.stderr
    assert not (isolated_env / "config.json").exists()


def test_browser_login_replaces_a_client_registered_for_the_old_mcp_scope(
    httpx_mock: HTTPXMock,
    invoke: Any,
    oauth_server: None,
    fake_browser: list[dict[str, str]],
    isolated_env: Path,
) -> None:
    _write_host_config(
        isolated_env,
        token="old-oauth-token",
        token_type="oauth",
        client_id="old-mcp-client",
        scope="mcp",
    )

    result = invoke("--host", HOST, "auth", "login", "--base-url", BASE_URL)

    assert result.exit_code == 0, result.stderr
    registrations = [request for request in httpx_mock.get_requests() if request.url.path == "/oauth2/register"]
    assert len(registrations) == 1


def test_an_unsupported_scope_is_refused_before_the_browser_opens(
    invoke: Any, oauth_server: None, fake_browser: list[dict[str, str]]
) -> None:
    result = invoke("--host", HOST, "auth", "login", "--base-url", BASE_URL, "--scope", "nope")

    assert result.exit_code == 4
    assert "not offered" in result.stderr
    assert fake_browser == []


def test_a_mismatched_state_is_rejected(invoke: Any, oauth_server: None, monkeypatch: pytest.MonkeyPatch) -> None:
    # A redirect we did not initiate must not have its code exchanged.
    _browser_that_redirects_to("code=stolen&state=not-the-state-we-sent", monkeypatch)

    result = invoke("--host", HOST, "auth", "login", "--base-url", BASE_URL)

    assert result.exit_code == 4
    assert "unexpected 'state'" in result.stderr


def test_a_code_is_not_exchanged_after_a_state_mismatch(
    httpx_mock: HTTPXMock, invoke: Any, oauth_server: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    _browser_that_redirects_to("code=stolen&state=wrong", monkeypatch)

    invoke("--host", HOST, "auth", "login", "--base-url", BASE_URL)

    assert [r for r in httpx_mock.get_requests() if r.url.path == "/oauth2/token"] == []


def test_an_error_redirect_is_reported(invoke: Any, oauth_server: None, monkeypatch: pytest.MonkeyPatch) -> None:
    _browser_that_redirects_to("error=access_denied&error_description=User+said+no", monkeypatch)

    result = invoke("--host", HOST, "auth", "login", "--base-url", BASE_URL)

    assert result.exit_code == 4
    assert "User said no" in result.stderr


def test_pat_login_reads_stdin_and_verifies_it(httpx_mock: HTTPXMock, invoke: Any, isolated_env: Path) -> None:
    httpx_mock.add_response(url=f"{BASE_URL}/api/users/me", json=quota())

    result = invoke("--host", HOST, "auth", "login", "--with-token", "--base-url", BASE_URL, stdin="pat-secret\n")

    assert result.exit_code == 0, result.stderr
    stored = json.loads((isolated_env / "config.json").read_text())["hosts"][HOST]
    assert (stored["token"], stored["token_type"]) == ("pat-secret", "pat")


def test_pat_login_keeps_nothing_from_an_earlier_oauth_login(
    httpx_mock: HTTPXMock, invoke: Any, isolated_env: Path
) -> None:
    """A stored entry must describe the token it holds, not the one before it."""
    _write_host_config(
        isolated_env,
        token="oauth-access-token",
        token_type="oauth",
        expires_at=4102444800,
        client_id="cli-client-1",
        scope="mcp",
    )
    httpx_mock.add_response(url=f"{BASE_URL}/api/users/me", json=quota())

    result = invoke("--host", HOST, "auth", "login", "--with-token", "--base-url", BASE_URL, stdin="pat-secret\n")

    assert result.exit_code == 0, result.stderr
    stored = json.loads((isolated_env / "config.json").read_text())["hosts"][HOST]
    assert stored == {"base_url": BASE_URL, "token": "pat-secret", "token_type": "pat"}


def test_pat_login_rejects_a_token_the_server_refuses(httpx_mock: HTTPXMock, invoke: Any, isolated_env: Path) -> None:
    httpx_mock.add_response(url=f"{BASE_URL}/api/users/me", status_code=401, json={"detail": "nope"})

    result = invoke("--host", HOST, "auth", "login", "--with-token", "--base-url", BASE_URL, stdin="bad\n")

    assert result.exit_code == 4
    assert "personal access token" in result.stderr
    assert "OAuth" not in result.stderr
    assert not (isolated_env / "config.json").exists()


def test_browser_login_explains_an_oauth_verification_failure(
    httpx_mock: HTTPXMock,
    invoke: Any,
    fake_browser: list[dict[str, str]],
    isolated_env: Path,
) -> None:
    _mock_oauth_discovery_and_registration(httpx_mock)
    httpx_mock.add_response(
        url=f"{BASE_URL}/oauth2/token",
        json={"access_token": "wrong-audience-token", "token_type": "Bearer", "expires_in": 900},
    )
    httpx_mock.add_response(url=f"{BASE_URL}/api/users/me", status_code=401, json={"detail": "Invalid token audience"})

    result = invoke("--host", HOST, "auth", "login", "--base-url", BASE_URL)

    assert result.exit_code == 4
    assert "OAuth access token" in result.stderr
    assert "audience or scopes" in result.stderr
    assert "personal access token is still valid" not in result.stderr
    assert not (isolated_env / "config.json").exists()


def test_login_reports_api_connection_failures_separately(httpx_mock: HTTPXMock, invoke: Any) -> None:
    httpx_mock.add_exception(httpx.ConnectError("connection refused"), url=f"{BASE_URL}/api/users/me")

    result = invoke("--host", HOST, "auth", "login", "--with-token", "--base-url", BASE_URL, stdin="pat\n")

    assert result.exit_code == 4
    assert "could not verify credentials" in result.stderr
    assert "--base-url" in result.stderr
    assert "personal access token" not in result.stderr


def test_login_does_not_treat_an_api_redirect_as_success(
    httpx_mock: HTTPXMock, invoke: Any, isolated_env: Path
) -> None:
    httpx_mock.add_response(
        url=f"{BASE_URL}/api/users/me",
        status_code=302,
        headers={"Location": f"{BASE_URL}/login"},
    )

    result = invoke("--host", HOST, "auth", "login", "--with-token", "--base-url", BASE_URL, stdin="pat\n")

    assert result.exit_code == 4
    assert "unexpected HTTP" in result.stderr
    assert "302" in result.stderr
    assert "--base-url" in result.stderr
    assert not (isolated_env / "config.json").exists()


def test_pat_login_rejects_empty_stdin(invoke: Any) -> None:
    result = invoke("--host", HOST, "auth", "login", "--with-token", "--base-url", BASE_URL, stdin="\n")
    assert result.exit_code == 4
    assert "no token" in result.stderr


def test_status_exits_nonzero_when_nothing_is_stored(invoke: Any) -> None:
    result = invoke("auth", "status")
    assert result.exit_code == 1
    assert "not logged in" in result.stdout


def test_status_confirms_a_working_token(httpx_mock: HTTPXMock, invoke: Any, logged_in: Path) -> None:
    httpx_mock.add_response(url=f"{BASE_URL}/api/users/me", json=quota())

    result = invoke("auth", "status")

    assert result.exit_code == 0
    assert "token accepted" in result.stdout


def test_status_flags_a_rejected_token(httpx_mock: HTTPXMock, invoke: Any, logged_in: Path) -> None:
    httpx_mock.add_response(url=f"{BASE_URL}/api/users/me", status_code=401, json={})

    result = invoke("auth", "status")

    assert "rejected" in result.stdout


def test_status_never_prints_the_whole_token(invoke: Any, logged_in: Path, httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(url=f"{BASE_URL}/api/users/me", json=quota())

    result = invoke("auth", "status")

    assert TOKEN not in result.stdout
    assert TOKEN[:6] in result.stdout


def test_auth_token_prints_the_token_bare(invoke: Any, logged_in: Path) -> None:
    result = invoke("auth", "token")

    assert result.exit_code == 0
    assert result.stdout.strip() == TOKEN


def test_logout_removes_the_host(invoke: Any, logged_in: Path, isolated_env: Path) -> None:
    result = invoke("auth", "logout", "--yes")

    assert result.exit_code == 0
    assert Config.load(isolated_env / "config.json").hosts == {}


def test_logout_revokes_an_oauth_token_for_the_api_audience(
    httpx_mock: HTTPXMock, invoke: Any, isolated_env: Path
) -> None:
    _write_host_config(
        isolated_env,
        token="oauth-access-token",
        token_type="oauth",
        client_id="cli-client-1",
        audience="jsonhub-api",
        scope="jsonhub:entities:read jsonhub:entities:write jsonhub:definitions:write",
    )
    httpx_mock.add_response(url=f"{BASE_URL}/.well-known/oauth-authorization-server", json=METADATA)
    httpx_mock.add_response(url=f"{BASE_URL}/oauth2/revoke", json={})

    result = invoke("--host", HOST, "auth", "logout", "--yes")

    assert result.exit_code == 0, result.stderr
    revocation = next(request for request in httpx_mock.get_requests() if request.url.path == "/oauth2/revoke")
    body = {key: values[0] for key, values in parse_qs(revocation.content.decode()).items()}
    assert body["audience"] == "jsonhub-api"


def test_logout_keeps_the_legacy_mcp_revocation_audience(
    httpx_mock: HTTPXMock, invoke: Any, isolated_env: Path
) -> None:
    _write_host_config(
        isolated_env,
        token="legacy-mcp-token",
        token_type="oauth",
        client_id="legacy-client",
        scope="mcp",
    )
    httpx_mock.add_response(url=f"{BASE_URL}/.well-known/oauth-authorization-server", json=METADATA)
    httpx_mock.add_response(url=f"{BASE_URL}/oauth2/revoke", json={})

    result = invoke("--host", HOST, "auth", "logout", "--yes")

    assert result.exit_code == 0, result.stderr
    revocation = next(request for request in httpx_mock.get_requests() if request.url.path == "/oauth2/revoke")
    body = {key: values[0] for key, values in parse_qs(revocation.content.decode()).items()}
    assert body["audience"] == "mcp"


def test_logout_on_a_clean_machine_is_not_an_error(invoke: Any) -> None:
    result = invoke("auth", "logout", "--yes")
    assert result.exit_code == 0
    assert "nothing to do" in result.stderr


def test_pkce_pair_is_url_safe_and_long_enough() -> None:
    verifier, challenge = oauth.pkce_pair()
    # RFC 7636 requires 43-128 unreserved characters.
    assert 43 <= len(verifier) <= 128
    assert set(verifier) <= set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~")
    assert "=" not in challenge
