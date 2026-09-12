"""Entry point: the ``jsonhub`` root command.

Also the single place where :class:`~jsonhub_cli.errors.JsonHubCliError` is
turned into a message on stderr and an exit code, so no command needs its own
try/except and no user ever sees a traceback for an expected failure.
"""

from __future__ import annotations

import ssl
import sys
from collections.abc import Sequence
from typing import Annotated

import httpx
import typer
from jsonhub_sdk import errors as sdk_errors

from . import __version__, output
from ._shared_docs import EPILOG
from .commands import auth, definition, entity, token
from .commands import config as config_cmd
from .commands import me as me_cmd
from .commands._shared import CliState
from .errors import CertificateTrustError, JsonHubCliError, ValidationError

app = typer.Typer(
    name="jsonhub",
    help="Work with JsonHub from the command line.",
    epilog=EPILOG,
    no_args_is_help=True,
    add_completion=True,
    rich_markup_mode="markdown",
    context_settings={"help_option_names": ["-h", "--help"]},
)

app.add_typer(auth.app, name="auth")
app.add_typer(entity.app, name="entity")
app.add_typer(definition.app, name="definition")
app.add_typer(token.app, name="token")
app.add_typer(config_cmd.app, name="config")
app.command("me")(me_cmd.me)


def _is_certificate_verification_error(exc: BaseException) -> bool:
    """Return whether an exception chain contains a TLS trust failure."""
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        if isinstance(current, ssl.SSLCertVerificationError):
            return True
        seen.add(id(current))
        current = current.__cause__ or current.__context__
    return False


def _version_callback(value: bool) -> None:
    if value:
        print(f"jsonhub {__version__}")
        raise typer.Exit


@app.callback()
def root(
    ctx: typer.Context,
    host: Annotated[
        str | None,
        typer.Option(
            "--host",
            help="JsonHub deployment to talk to. Defaults to the configured host, or $JSONHUB_HOST.",
            metavar="HOST",
        ),
    ] = None,
    insecure: Annotated[
        bool,
        typer.Option("--insecure", help="Disable TLS certificate verification for this command."),
    ] = False,
    _version: Annotated[
        bool,
        typer.Option("--version", "-v", callback=_version_callback, is_eager=True, help="Print the version and exit."),
    ] = False,
) -> None:
    """Set up state every subcommand shares."""
    cli = CliState(host=host, insecure=True if insecure else None)
    effective = cli.config.host_config(host, insecure=cli.insecure)
    if effective.insecure:
        output.warn(f"TLS certificate verification is disabled for {cli.config.resolve_host(host)}")
    ctx.obj = cli


def run(argv: Sequence[str] | None = None) -> int:
    """Run the CLI and return its exit code, reporting expected failures.

    Typer runs in its own standalone mode so that usage errors, ``--help`` and
    Ctrl-C keep Click's familiar behaviour; those arrive here as ``SystemExit``.
    The CLI's own exceptions are reported as a single stderr line rather than a
    traceback.

    Exit codes: 0 success, 1 generic failure, 2 bad input, 3 not found,
    4 authentication required, 22 payload rejected by the API.

    This, not :func:`main`, is the real error boundary, so tests can drive the
    same handling the installed script gets.
    """
    try:
        app(args=list(argv) if argv is not None else None, prog_name="jsonhub")
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else 0
    except ValidationError as exc:
        output.fail(exc.message)
        for path, message in exc.violations:
            output.err.print(f"  [dim]{path or '(root)'}:[/dim] {message}")
        return exc.exit_code
    except JsonHubCliError as exc:
        output.fail(exc.message, hint=exc.hint)
        return exc.exit_code
    except sdk_errors.UnexpectedStatus as exc:
        output.fail(f"the API returned an unexpected status {exc.status_code}")
        return 1
    except KeyError as exc:
        # The generated models read every required field with d.pop(), so a body
        # that omits one -- typically a deployment older than the SDK this
        # release is built against -- surfaces as a bare KeyError from inside
        # sync_detailed rather than as an HTTP error. Report it, don't traceback.
        field = exc.args[0] if exc.args else "a required field"
        output.fail(
            f"the API returned a response with no '{field}'",
            hint="the deployment may be older than this jsonhub release; check --host",
        )
        return 1
    except httpx.HTTPError as exc:
        if _is_certificate_verification_error(exc):
            host = exc.request.url.netloc.decode("ascii")
            error = CertificateTrustError(host, str(exc))
            output.fail(error.message, hint=error.hint)
            return error.exit_code
        # Connection refused, DNS failure, timeout...
        output.fail(f"could not reach the API: {exc}", hint="check --host and your network connection")
        return 1
    return 0


def main() -> None:
    sys.exit(run())


if __name__ == "__main__":
    main()
