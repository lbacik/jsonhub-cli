"""``jsonhub auth`` - log in, log out, and inspect stored credentials."""

from __future__ import annotations

import sys
from dataclasses import replace
from typing import Annotated

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


@app.command("login")
def login(
    ctx: typer.Context,
    with_token: Annotated[
        bool,
        typer.Option("--with-token", help="Read a personal access token from stdin instead of using a browser."),
    ] = False,
    scope: Annotated[
        str | None,
        typer.Option("--scope", help="OAuth scope to request. Defaults to the server's first supported scope."),
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
    existing = cli.config.hosts.get(host) or HostConfig(base_url=base_url_for(host))
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
            anon_config,
            scope=scope,
            open_browser=not no_browser,
            timeout=timeout,
        )

    if not _verify(entry):
        raise AuthError(
            f"{host} rejected the new credentials",
            hint="check --base-url, or that the personal access token is still valid",
        )

    cli.config.set_host_config(host, entry)
    if not cli.config.hosts.get(cli.config.default_host):
        cli.config.default_host = host
    cli.config.save()

    output.success(f"Logged in to {host}")
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
    # The flow must not send credentials; the caller hands us a config that
    # carries none, so the client built from it carries none either.
    tokens, resolved_client_id, _redirect_uri = oauth.login(
        anonymous_client(cfg),
        scope=scope,
        open_browser=open_browser,
        timeout=timeout,
        client_id=cfg.client_id,
    )
    return replace(
        cfg,
        token=tokens.access_token,
        token_type="oauth",
        expires_at=tokens.expires_at,
        client_id=resolved_client_id,
        scope=tokens.scope or scope,
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

    confirm(f"Log out of {host}?", assume_yes=yes)

    if entry.token_type == "oauth" and entry.client_id:
        anon = anonymous_client(entry)
        try:
            meta = oauth.fetch_metadata(anon)
        except AuthError:
            meta = None
        if meta and meta.revocation_endpoint:
            if oauth.revoke(anon, token=entry.token, client_id=entry.client_id):
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
            if _verify(entry):
                output.out.print("    [green]token accepted by the server[/green]")
            else:
                output.out.print("    [yellow]token was rejected by the server[/yellow]")

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


def _verify(cfg: HostConfig) -> bool:
    """Check a host config's token by calling ``/api/users/me``.

    The endpoint reports quota, not identity -- JsonHub exposes no "who am I"
    for the current user -- so this is purely "does the server accept this
    token", which is the useful thing to tell the user after a login.

    Only the status code matters, so the request goes through the SDK client's
    httpx client rather than the generated endpoint: a login check must not
    fail because a deployment's quota payload does not match the schema.
    """
    client = build_client(cfg)
    try:
        response = client.get_httpx_client().get(WHOAMI_PATH)
    except httpx.HTTPError:
        return False
    return response.status_code < 400
