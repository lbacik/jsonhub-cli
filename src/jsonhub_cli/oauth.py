"""Browser-based login: OAuth 2.0 authorization code flow with PKCE.

The CLI is a *public* client, so there is no secret to keep: security comes from
PKCE (RFC 7636) plus a redirect URI on the loopback interface (RFC 8252). The
sequence is

1. read ``/.well-known/oauth-authorization-server`` for the real endpoints;
2. bind a loopback port, so the exact redirect URI is known before registering;
3. dynamically register a public client for that redirect URI (RFC 7591);
4. open the authorization URL in the user's browser;
5. accept the single redirect on the loopback socket, checking ``state``;
6. exchange the code plus ``code_verifier`` for an access token.

Only step 4 leaves the machine's control, and the token itself never travels
through the browser -- the code does, and it is useless without the verifier.
"""

from __future__ import annotations

import base64
import hashlib
import http.server
import secrets
import socket
import threading
import time
import webbrowser
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qs, urlencode, urlsplit

from jsonhub_sdk import Client
from jsonhub_sdk.api.oauth2 import oauth2_metadata, oauth2_register, oauth2_revoke, oauth2_token
from jsonhub_sdk.models import (
    Oauth2RegisterBody,
    Oauth2RegisterBodyTokenEndpointAuthMethod,
    Oauth2RegisterResponse201,
    Oauth2RevokeBody,
    Oauth2TokenBody,
    Oauth2TokenBodyGrantType,
    Oauth2TokenResponse200,
)

from .api import unset_to_none
from .errors import AuthError
from .output import note

CLIENT_NAME = "jsonhub CLI"
API_AUDIENCE = "jsonhub-api"
LEGACY_DEFAULT_AUDIENCE = "mcp"
CLI_SCOPES = (
    "jsonhub:entities:read",
    "jsonhub:entities:write",
    "jsonhub:definitions:write",
)
DEFAULT_CLI_SCOPE = " ".join(CLI_SCOPES)
#: Tried first so a repeat login can reuse a stored client registration.
PREFERRED_PORT = 8976
CALLBACK_PATH = "/callback"
DEFAULT_LOGIN_TIMEOUT = 300.0

_SUCCESS_HTML = b"""<!doctype html>
<title>Logged in</title>
<style>body{font:16px/1.5 system-ui,sans-serif;margin:4rem auto;max-width:26rem;text-align:center}
h1{font-size:1.25rem}</style>
<h1>You are logged in to JsonHub</h1>
<p>You can close this tab and return to your terminal.</p>
"""

_FAILURE_HTML = b"""<!doctype html>
<title>Login failed</title>
<style>body{font:16px/1.5 system-ui,sans-serif;margin:4rem auto;max-width:26rem;text-align:center}
h1{font-size:1.25rem;color:#b00}</style>
<h1>Login failed</h1>
<p>Return to your terminal for details.</p>
"""


@dataclass(frozen=True)
class Metadata:
    """The subset of the authorization server metadata the flow needs."""

    authorization_endpoint: str
    token_endpoint: str
    registration_endpoint: str | None
    revocation_endpoint: str | None
    scopes_supported: list[str]
    audiences_supported: list[str]


@dataclass(frozen=True)
class TokenSet:
    """A freshly issued access token, normalised for storage in the config."""

    access_token: str
    token_type: str
    scope: str | None
    expires_at: int | None


def fetch_metadata(client: Client) -> Metadata:
    """Discover the authorization server's endpoints."""
    response = oauth2_metadata.sync_detailed(client=client)
    if response.status_code >= 400 or response.parsed is None:
        raise AuthError(
            f"{client._base_url} did not return OAuth metadata (HTTP {response.status_code})",
            hint="check --host, or log in with a personal access token: jsonhub auth login --with-token",
        )
    meta = response.parsed
    authorize = unset_to_none(meta.authorization_endpoint)
    token = unset_to_none(meta.token_endpoint)
    if not authorize or not token:
        raise AuthError("the server's OAuth metadata is missing an authorization or token endpoint")
    return Metadata(
        authorization_endpoint=authorize,
        token_endpoint=token,
        registration_endpoint=unset_to_none(meta.registration_endpoint),
        revocation_endpoint=unset_to_none(meta.revocation_endpoint),
        scopes_supported=list(unset_to_none(meta.scopes_supported) or []),
        audiences_supported=list(unset_to_none(meta.audiences_supported) or []),
    )


def pkce_pair() -> tuple[str, str]:
    """Return ``(verifier, challenge)`` for the S256 PKCE method."""
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(48)).decode().rstrip("=")
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).decode().rstrip("=")
    return verifier, challenge


def register_client(client: Client, redirect_uri: str, scope: str | None) -> str:
    """Dynamically register a public client and return its ``client_id``."""
    body = Oauth2RegisterBody(
        redirect_uris=[redirect_uri],
        client_name=CLIENT_NAME,
        grant_types=["authorization_code"],
        response_types=["code"],
        token_endpoint_auth_method=Oauth2RegisterBodyTokenEndpointAuthMethod.NONE,
    )
    if scope:
        body.scope = scope
    response = oauth2_register.sync_detailed(client=client, body=body)
    parsed = response.parsed
    if isinstance(parsed, Oauth2RegisterResponse201):
        client_id = unset_to_none(parsed.client_id)
        if client_id:
            return client_id
    message = _oauth_error(response.parsed) or f"HTTP {response.status_code}"
    raise AuthError(f"could not register this CLI as an OAuth client: {message}")


def exchange_code(
    client: Client,
    *,
    code: str,
    redirect_uri: str,
    client_id: str,
    code_verifier: str,
) -> TokenSet:
    """Swap the authorization code for an access token."""
    body = Oauth2TokenBody(
        grant_type=Oauth2TokenBodyGrantType.AUTHORIZATION_CODE,
        code=code,
        redirect_uri=redirect_uri,
        client_id=client_id,
        code_verifier=code_verifier,
    )
    response = oauth2_token.sync_detailed(client=client, body=body)
    parsed = response.parsed
    if not isinstance(parsed, Oauth2TokenResponse200):
        message = _oauth_error(parsed) or f"HTTP {response.status_code}"
        raise AuthError(f"the token exchange was rejected: {message}")
    access_token = unset_to_none(parsed.access_token)
    if not access_token:
        raise AuthError("the token endpoint returned no access token")
    expires_in = unset_to_none(parsed.expires_in)
    return TokenSet(
        access_token=access_token,
        token_type=unset_to_none(parsed.token_type) or "Bearer",
        scope=unset_to_none(parsed.scope),
        expires_at=int(time.time()) + expires_in if expires_in else None,
    )


def revoke(client: Client, *, token: str, client_id: str, audience: str) -> bool:
    """Best-effort server-side revocation; ``False`` if the server declined."""
    body = Oauth2RevokeBody(token=token, client_id=client_id, audience=audience)
    response = oauth2_revoke.sync_detailed(client=client, body=body)
    return response.status_code < 400


def login(
    client: Client,
    *,
    scope: str | None = None,
    open_browser: bool = True,
    timeout: float = DEFAULT_LOGIN_TIMEOUT,
    client_id: str | None = None,
) -> tuple[TokenSet, str, str]:
    """Run the whole browser flow, returning ``(tokens, client_id, redirect_uri)``.

    ``client_id`` may be a previously stored registration; it is only reused
    when the preferred port -- and hence the exact redirect URI it was
    registered for -- is still available.
    """
    meta = fetch_metadata(client)
    if API_AUDIENCE not in meta.audiences_supported:
        raise AuthError(
            f"{client._base_url} does not advertise the '{API_AUDIENCE}' OAuth audience",
            hint="upgrade the server, or log in with a personal access token: jsonhub auth login --with-token",
        )

    scope = scope or DEFAULT_CLI_SCOPE
    requested_scopes = scope.split()
    unsupported_scopes = [value for value in requested_scopes if value not in meta.scopes_supported]
    if unsupported_scopes:
        raise AuthError(
            f"scope '{unsupported_scopes[0]}' is not offered by {client._base_url}",
            hint=f"supported scopes: {', '.join(meta.scopes_supported)}",
        )

    listener, port = _bind_loopback(PREFERRED_PORT)
    try:
        redirect_uri = f"http://127.0.0.1:{port}{CALLBACK_PATH}"
        if client_id is None or port != PREFERRED_PORT:
            if not meta.registration_endpoint:
                raise AuthError(
                    "the server does not support dynamic client registration",
                    hint="log in with a personal access token instead: jsonhub auth login --with-token",
                )
            client_id = register_client(client, redirect_uri, scope)

        verifier, challenge = pkce_pair()
        state = secrets.token_urlsafe(24)
        params = {
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "state": state,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "resource": API_AUDIENCE,
        }
        if scope:
            params["scope"] = scope
        authorize_url = f"{meta.authorization_endpoint}?{urlencode(params)}"

        note(f"Opening {authorize_url}")
        if open_browser and not webbrowser.open(authorize_url):
            note("Could not launch a browser; open the URL above manually.")

        code = _await_callback(listener, expected_state=state, timeout=timeout)
    finally:
        listener.close()

    tokens = exchange_code(
        client,
        code=code,
        redirect_uri=redirect_uri,
        client_id=client_id,
        code_verifier=verifier,
    )
    granted_scopes = set(tokens.scope.split()) if tokens.scope else set(requested_scopes)
    if granted_scopes != set(requested_scopes):
        raise AuthError(
            "the token endpoint did not grant the requested OAuth scopes",
            hint=f"requested: {', '.join(requested_scopes)}; granted: {', '.join(sorted(granted_scopes)) or 'none'}",
        )
    return tokens, client_id, redirect_uri


def _bind_loopback(preferred: int) -> tuple[socket.socket, int]:
    """Bind the preferred loopback port, falling back to an ephemeral one."""
    for port in (preferred, 0):
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind(("127.0.0.1", port))
        except OSError:
            sock.close()
            continue
        sock.listen(1)
        return sock, sock.getsockname()[1]
    raise AuthError("could not open a local port to receive the login redirect")


class _CallbackServer(http.server.HTTPServer):
    """Single-shot HTTP server that carries the captured query back out."""

    def __init__(self, listener: socket.socket, timeout: float) -> None:
        super().__init__(("127.0.0.1", 0), _CallbackHandler, bind_and_activate=False)
        # Reuse the already-bound socket: the port had to be known before the
        # client was registered, so binding cannot happen here.
        self.socket = listener
        self.server_address = listener.getsockname()
        self.timeout = timeout
        self.result: dict[str, str] = {}


class _CallbackHandler(http.server.BaseHTTPRequestHandler):
    """Handles exactly the one redirect the authorization server sends back."""

    server_version = "jsonhub-cli"
    server: _CallbackServer

    def do_GET(self) -> None:
        parts = urlsplit(self.path)
        if parts.path.rstrip("/") not in {CALLBACK_PATH.rstrip("/"), ""}:
            self.send_error(404)
            return
        query = {k: v[0] for k, v in parse_qs(parts.query).items() if v}
        self.server.result = query
        ok = "code" in query and "error" not in query
        body = _SUCCESS_HTML if ok else _FAILURE_HTML
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: Any) -> None:
        """Silence the default stderr access log."""


def _await_callback(listener: socket.socket, *, expected_state: str, timeout: float) -> str:
    """Serve one redirect and return the authorization code."""
    server = _CallbackServer(listener, timeout)

    done = threading.Event()

    def serve() -> None:
        # handle_request() honours server.timeout and returns either way, so a
        # user who abandons the browser tab still gets a clean error.
        server.handle_request()
        done.set()

    worker = threading.Thread(target=serve, daemon=True)
    worker.start()
    note("Waiting for the browser to complete the login...")
    if not done.wait(timeout + 5):
        raise AuthError(f"timed out after {int(timeout)}s waiting for the login redirect")

    query = server.result
    if not query:
        raise AuthError(f"timed out after {int(timeout)}s waiting for the login redirect")
    if "error" in query:
        detail = query.get("error_description") or query["error"]
        raise AuthError(f"the authorization server refused the login: {detail}")
    if not secrets.compare_digest(query.get("state", ""), expected_state):
        # A mismatched state means the redirect did not come from the request
        # we started; treat it as a CSRF attempt and discard the code.
        raise AuthError("the login redirect carried an unexpected 'state' value; aborting")
    code = query.get("code")
    if not code:
        raise AuthError("the login redirect carried no authorization code")
    return code


def _oauth_error(parsed: Any) -> str | None:
    """Format an ``{error, error_description}`` OAuth error response."""
    if parsed is None:
        return None
    error = unset_to_none(getattr(parsed, "error", None))
    description = unset_to_none(getattr(parsed, "error_description", None))
    if error and description:
        return f"{error}: {description}"
    return error or description or None
