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
    "scopes_supported": ["mcp", "frontend"],
}


@pytest.fixture
def oauth_server(httpx_mock: HTTPXMock) -> None:
    """Mock the four endpoints a successful browser login touches."""
    httpx_mock.add_response(url=f"{BASE_URL}/.well-known/oauth-authorization-server", json=METADATA)
    httpx_mock.add_response(url=f"{BASE_URL}/oauth2/register", status_code=201, json={"client_id": "cli-client-1"})
    httpx_mock.add_response(
        url=f"{BASE_URL}/oauth2/token",
        json={"access_token": "oauth-access-token", "token_type": "Bearer", "expires_in": 3600, "scope": "mcp"},
    )
    httpx_mock.add_response(url=f"{BASE_URL}/api/users/me", json=quota())


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
    invoke: Any, oauth_server: None, fake_browser: list[dict[str, str]], isolated_env: Path
) -> None:
    result = invoke("--host", HOST, "auth", "login", "--base-url", BASE_URL)

    assert result.exit_code == 0, result.stderr
    stored = json.loads((isolated_env / "config.json").read_text())["hosts"][HOST]
    assert stored["token"] == "oauth-access-token"
    assert stored["token_type"] == "oauth"
    assert stored["client_id"] == "cli-client-1"


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


def test_login_requests_the_servers_first_supported_scope(
    invoke: Any, oauth_server: None, fake_browser: list[dict[str, str]]
) -> None:
    invoke("--host", HOST, "auth", "login", "--base-url", BASE_URL)
    assert fake_browser[0]["scope"] == "mcp"


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


def test_pat_login_rejects_a_token_the_server_refuses(httpx_mock: HTTPXMock, invoke: Any, isolated_env: Path) -> None:
    httpx_mock.add_response(url=f"{BASE_URL}/api/users/me", status_code=401, json={"detail": "nope"})

    result = invoke("--host", HOST, "auth", "login", "--with-token", "--base-url", BASE_URL, stdin="bad\n")

    assert result.exit_code == 4
    assert "rejected the new credentials" in result.stderr
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
