"""``jsonhub config`` - inspect and edit CLI settings."""

from __future__ import annotations

from typing import Annotated

import typer

from .. import output
from ..config import HostConfig, base_url_for, normalize_host
from ..errors import ConfigError
from ._shared import JsonFlag, state

app = typer.Typer(no_args_is_help=True, help="Inspect and change jsonhub's own settings.")


@app.command("list")
def list_config(ctx: typer.Context, as_json: JsonFlag = False) -> None:
    """Show the current configuration. Tokens are never printed."""
    cli = state(ctx)
    hosts = {
        host: {
            "base_url": entry.base_url,
            "insecure": entry.insecure,
            "logged_in": bool(entry.token),
            "scope": entry.scope,
        }
        for host, entry in sorted(cli.config.hosts.items())
    }
    document = {"path": str(cli.config.path), "default_host": cli.config.default_host, "hosts": hosts}

    if as_json:
        output.print_json(document)
        return

    output.print_fields([("config file", str(cli.config.path)), ("default host", cli.config.default_host)])
    if hosts:
        output.err.print()
        output.print_table(
            ["HOST", "BASE URL", "INSECURE", "LOGGED IN", "SCOPE"],
            (
                [host, values["base_url"], values["insecure"], values["logged_in"], values["scope"]]
                for host, values in hosts.items()
            ),
        )


@app.command("set-host")
def set_host(
    ctx: typer.Context,
    host: Annotated[str, typer.Argument(help="Hostname of the JsonHub deployment.")],
    base_url: Annotated[
        str | None,
        typer.Option("--base-url", help="API base URL. Defaults to https://<host>."),
    ] = None,
    make_default: Annotated[
        bool,
        typer.Option("--default", help="Also make this the default host."),
    ] = False,
    insecure: Annotated[
        bool | None,
        typer.Option("--insecure/--no-insecure", help="Store whether this host skips TLS certificate verification."),
    ] = None,
) -> None:
    """Register a host, or change its base URL.

    Only endpoint settings are touched; run 'jsonhub auth login' to add
    credentials for the host.
    """
    cli = state(ctx)
    key = normalize_host(host)
    entry = cli.config.hosts.get(key)
    resolved = base_url or (entry.base_url if entry else base_url_for(key))

    if entry is None:
        entry = HostConfig(base_url=resolved)
    else:
        entry.base_url = resolved
    if insecure is not None:
        entry.insecure = insecure
    cli.config.set_host_config(key, entry)

    if make_default or not cli.config.hosts.get(cli.config.default_host):
        cli.config.default_host = key
    cli.config.save()

    output.success(f"Host {key} points at {resolved}")
    if cli.config.default_host == key:
        output.note("This is now the default host")


@app.command("set-default")
def set_default(
    ctx: typer.Context,
    host: Annotated[str, typer.Argument(help="Hostname to use when --host is not given.")],
) -> None:
    """Choose the host commands talk to by default."""
    cli = state(ctx)
    key = normalize_host(host)
    if key not in cli.config.hosts:
        raise ConfigError(
            f"{key} is not configured",
            hint=f"add it with 'jsonhub config set-host {key}' or 'jsonhub auth login --host {key}'",
        )
    cli.config.default_host = key
    cli.config.save()
    output.success(f"Default host is now {key}")
