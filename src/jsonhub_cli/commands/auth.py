"""``jsonhub auth`` - log in, log out, and inspect stored credentials."""

from __future__ import annotations

import sys
from dataclasses import dataclass, replace
from typing import Annotated, Any

import httpx
import typer

from .. import oauth, output
from ..api import anonymous_client, build_client
from ..config import HostConfig, base_url_for
from ..errors import AuthError
from ._shared import YesFlag, confirm, state

app = typer.Typer(no_args_is_help=True, help="Authenticate jsonhub with a JsonHub deployment.")

TOKEN_PREVIEW_CHARS = 6
WHOAMI_PATH = "/api/users/me"


@dataclass(frozen=True)
class Account:
    """Who ``/api/users/me`` says a credential belongs to."""

    id: str | None
    email: str | None

    def __str__(self) -> str:
        return self.email or self.id or "an unnamed account"


@app.command("login")
def login(
    ctx: typer.Context,
    with_token: Annotated[
        bool,
        typer.Option("--with-token", help="Read a personal access token from stdin instead of using a browser."),
    ] = False,
    scope: Annotated[
        str | None,
        typer.Option("--scope", help="OAuth scopes to request. Defaults to the capabilities used by the CLI."),
    ] = None,
    no_browser: Annotated[
        bool,
        typer.Option("--no-browser", help="Print the authorization URL instead of opening a browser."),
    ] = False,
    timeout: Annotated[
        float,
        typer.Option("--timeout", min=5.0, help="Seconds to wait for the browser to complete the login."),
    ] = oauth.DEFAULT_LOGIN_TIMEOUT,
    base_url: Annotated[
        str | None,
        typer.Option("--base-url", help="Override the API base URL for this host."),
    ] = None,
) -> None:
    """Log in to JsonHub.

    By default this opens a browser and runs the OAuth 2.0 authorization code
    flow with PKCE. With --with-token it instead reads a personal access token
    from stdin, which is what you want in CI:

        echo "$JSONHUB_PAT" | jsonhub auth login --with-token
    """
    cli = state(ctx)
    host = cli.config.resolve_host(cli.host)
    stored = cli.config.hosts.get(host) or HostConfig(base_url=base_url_for(host))
    existing = cli.config.host_config(cli.host, insecure=cli.insecure)
    if base_url:
        existing = replace(existing, base_url=base_url)

    # Log in from a clean, tokenless config: any stale credentials on file must
    # not influence (or be sent during) the flow, while the host's own endpoint
    # settings still apply.
    anon_config = existing.anonymous()

    if with_token:
        entry = _login_with_token(anon_config)
    else:
        entry = _login_with_browser(
            existing,
            scope=scope,
            open_browser=not no_browser,
            timeout=timeout,
        )

    account, verification_failure = _verify(entry)
    if verification_failure:
        raise verification_failure

    # An explicit command-line choice is the bootstrap path for a host whose
    # certificate is not trusted yet, so retain it with the credentials.
    # Environment overrides stay transient and must not quietly alter the
    # host's TLS policy on disk.
    persisted_insecure = cli.insecure if cli.insecure is not None else stored.insecure
    cli.config.set_host_config(host, replace(entry, insecure=persisted_insecure))
    if not cli.config.hosts.get(cli.config.default_host):
        cli.config.default_host = host
    cli.config.save()

    output.success(f"Logged in to {host}" + (f" as {account}" if account else ""))
    output.note(f"Credentials stored in {cli.config.path}")


def _login_with_token(cfg: HostConfig) -> HostConfig:
    if sys.stdin.isatty():
        raise AuthError(
            "--with-token expects the token on stdin",
            hint='pipe it in, e.g. echo "$JSONHUB_PAT" | jsonhub auth login --with-token',
        )
    token = sys.stdin.read().strip()
    if not token:
        raise AuthError("no token was read from stdin")
    # A personal access token has no OAuth registration behind it, so a
    # client_id left over from an earlier browser login is not this token's.
    return replace(cfg, token=token, token_type="pat", client_id=None)


def _login_with_browser(
    cfg: HostConfig,
    *,
    scope: str | None,
    open_browser: bool,
    timeout: float,
) -> HostConfig:
    requested_scope = scope or oauth.DEFAULT_CLI_SCOPE
    # A registration cannot gain scopes later. Re-register when the desired
    # capabilities differ, including migration from the old MCP default.
    reusable_client_id = cfg.client_id if cfg.scope == requested_scope else None
    # The flow must not send credentials. Only the prior registration metadata
    # above is reused; the client itself is built from an anonymous config.
    tokens, resolved_client_id, _redirect_uri = oauth.login(
        anonymous_client(cfg.anonymous()),
        scope=scope,
        open_browser=open_browser,
        timeout=timeout,
        client_id=reusable_client_id,
    )
    return replace(
        cfg,
        token=tokens.access_token,
        token_type="oauth",
        expires_at=tokens.expires_at,
        client_id=resolved_client_id,
        audience=oauth.API_AUDIENCE,
        scope=tokens.scope or requested_scope,
    )


@app.command("logout")
def logout(ctx: typer.Context, yes: YesFlag = False) -> None:
    """Remove stored credentials for a host, revoking the token if possible."""
    cli = state(ctx)
    host = cli.config.resolve_host(cli.host)
    entry = cli.config.hosts.get(host)
    if entry is None or not entry.token:
        output.note(f"Not logged in to {host}; nothing to do")
        return

    # Revocation must target the stored credential, while still honouring the
    # command's effective transport settings.  Environment credentials are
    # transient and must never be revoked or written by logout.
    transport = cli.config.host_config(cli.host, insecure=cli.insecure)
    entry = replace(entry, insecure=transport.insecure)
    assert entry.token is not None

    confirm(f"Log out of {host}?", assume_yes=yes)

    if entry.token_type == "oauth" and entry.client_id:
        anon = anonymous_client(entry)
        try:
            meta = oauth.fetch_metadata(anon)
        except AuthError:
            meta = None
        if meta and meta.revocation_endpoint:
            audience = entry.audience or oauth.LEGACY_DEFAULT_AUDIENCE
            if oauth.revoke(anon, token=entry.token, client_id=entry.client_id, audience=audience):
                output.note("Access token revoked server-side")
            else:
                # Local credentials still get dropped: the user asked to log out.
                output.warn("The server declined to revoke the token; removing it locally anyway")

    cli.config.remove_host(host)
    cli.config.save()
    output.success(f"Logged out of {host}")


@app.command("status")
def status(ctx: typer.Context) -> None:
    """Show which hosts you are logged in to, and verify the active one."""
    cli = state(ctx)
    active = cli.config.resolve_host(cli.host)
    hosts = dict(cli.config.hosts)
    if active not in hosts:
        hosts[active] = cli.config.host_config(cli.host)

    for host, entry in sorted(hosts.items()):
        if host == active:
            entry = cli.config.host_config(cli.host, insecure=cli.insecure)
        marker = "*" if host == active else " "
        output.out.print(f"{marker} [bold]{host}[/bold]  [dim]{entry.base_url}[/dim]")
        if not entry.token:
            output.out.print("    not logged in")
            continue
        kind = "personal access token" if entry.token_type == "pat" else "OAuth token"
        output.out.print(f"    token: {kind} ({_preview(entry.token)})")
        if entry.scope:
            output.out.print(f"    scope: {entry.scope}")
        if entry.is_expired:
            output.out.print("    [red]expired[/red] - run 'jsonhub auth login'")
        elif host == active:
            account, failure = _verify(entry)
            if failure is not None:
                output.out.print("    [yellow]token was rejected by the server[/yellow]")
            elif account is not None:
                output.out.print(f"    [green]logged in as {account}[/green]")
            else:
                output.out.print("    [green]token accepted by the server[/green]")

    if not any(entry.token for entry in hosts.values()):
        raise typer.Exit(1)


@app.command("token")
def print_token(ctx: typer.Context) -> None:
    """Print the stored access token, for piping into other tools."""
    cli = state(ctx)
    entry = cli.config.host_config(cli.host)
    if not entry.token:
        raise AuthError(
            f"not logged in to {cli.config.resolve_host(cli.host)}",
            hint="run 'jsonhub auth login'",
        )
    # Deliberately bare on stdout: this is meant for $(...) capture.
    print(entry.token)


def _preview(token: str) -> str:
    """Enough of a token to tell two apart, not enough to use."""
    return f"{token[:TOKEN_PREVIEW_CHARS]}..." if len(token) > TOKEN_PREVIEW_CHARS else "..."


def _verify(cfg: HostConfig) -> tuple[Account | None, AuthError | None]:
    """Ask ``/api/users/me`` whose credential this is, or why it was refused.

    The call goes through the raw httpx client on purpose: whether the server
    accepts the credential must not depend on its payload matching the SDK
    schema, so the identity is read leniently off the body and a response that
    does not carry one still counts as accepted.
    """
    client = build_client(cfg)
    try:
        response = client.get_httpx_client().get(WHOAMI_PATH)
    except httpx.HTTPError:
        return None, AuthError(
            f"could not verify credentials against {cfg.base_url}",
            hint="check --base-url and your network connection",
        )
    if response.is_success:
        return _account(response), None
    if response.status_code in {401, 403} and cfg.token_type == "oauth":
        return None, AuthError(
            f"the JsonHub API rejected the OAuth access token (HTTP {response.status_code})",
            hint="the authorization server may have issued it for the wrong audience or scopes",
        )
    if response.status_code in {401, 403}:
        return None, AuthError(
            f"the JsonHub API rejected the personal access token (HTTP {response.status_code})",
            hint="check that the personal access token is still valid",
        )
    return None, AuthError(
        f"could not verify credentials against {cfg.base_url}: unexpected HTTP {response.status_code}",
        hint="check --base-url and the server status",
    )


def _account(response: httpx.Response) -> Account | None:
    """Read the identity out of a ``/api/users/me`` body, if it carries one."""
    try:
        body = response.json()
    except ValueError:
        return None
    if not isinstance(body, dict):
        return None
    account = Account(id=_text(body.get("id")), email=_text(body.get("email")))
    return account if (account.id or account.email) else None


def _text(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None
