"""Bridge between the generated SDK and the CLI.

Two jobs live here:

* build an SDK client from the resolved :class:`~jsonhub_cli.config.HostConfig`;
* turn the SDK's ``Response[Union[Model, Error, ConstraintViolation]]`` unions
  into either a plain ``dict`` (the API's own JSON, camelCase keys intact) or a
  typed exception from :mod:`jsonhub_cli.errors`.

Commands therefore never branch on ``isinstance(parsed, Error)``; they get a
dict and let exceptions propagate to the top-level handler in ``main``.
"""

from __future__ import annotations

import json
from http import HTTPStatus
from typing import Any, Protocol, TypeVar

import httpx
from jsonhub_sdk import AuthenticatedClient, Client
from jsonhub_sdk.models import ConstraintViolation, Error
from jsonhub_sdk.types import Response, Unset

from .config import Config, HostConfig
from .errors import ApiError, AuthError, NotFoundError, ValidationError

T = TypeVar("T")

DEFAULT_TIMEOUT = 30.0

#: The API is content-negotiated (API Platform): it answers with JSON-LD by
#: default, a bare array for ``application/json``, and only returns the paginated
#: HAL envelope -- the one the generated collection models require -- for exactly
#: this media type. The OAuth endpoints ignore Accept and always answer JSON, so
#: one header serves every call.
HAL_MEDIA_TYPE = "application/hal+json"

#: Cap on how much of an unrecognised error body is echoed back to the user.
PROBLEM_TEXT_LIMIT = 200


class _Serializable(Protocol):
    def to_dict(self) -> dict[str, Any]: ...


def unset_to_none(value: T | Unset) -> T | None:
    """Collapse the SDK's ``UNSET`` sentinel to ``None``."""
    return None if isinstance(value, Unset) else value


def _client_kwargs(cfg: HostConfig, timeout: float) -> dict[str, Any]:
    """The transport settings every client shares, read off one host config.

    This is the single place a per-host transport setting has to be honoured:
    no code path builds a client from a bare base URL.
    """
    return {
        "base_url": cfg.base_url.rstrip("/"),
        "timeout": httpx.Timeout(timeout),
        "verify_ssl": not cfg.insecure,
        "raise_on_unexpected_status": False,
        "headers": {"Accept": HAL_MEDIA_TYPE},
    }


def anonymous_client(cfg: HostConfig, *, timeout: float = DEFAULT_TIMEOUT) -> Client:
    """A client that carries no credentials, but keeps the host's transport settings.

    Used for the OAuth flow, where sending a stale token would be wrong: any
    token on ``cfg`` is ignored by construction, so the caller need not scrub
    one first.
    """
    return Client(**_client_kwargs(cfg, timeout))


def build_client(cfg: HostConfig, *, timeout: float = DEFAULT_TIMEOUT) -> Client | AuthenticatedClient:
    """Return an authenticated client when a token is known, anonymous otherwise.

    Anonymous is a legitimate mode: public entities and definitions are
    readable without credentials, so ``jsonhub entity list`` works before login.
    """
    kwargs = _client_kwargs(cfg, timeout)
    if cfg.token:
        return AuthenticatedClient(token=cfg.token, **kwargs)
    return Client(**kwargs)


class Session:
    """Everything a command needs: config, chosen host, and a live SDK client.

    Created once by the root Typer callback and handed to subcommands through
    the Typer context, so ``--host`` is honoured uniformly.
    """

    def __init__(
        self, config: Config, host: str | None = None, *, insecure: bool | None = None, timeout: float = DEFAULT_TIMEOUT
    ) -> None:
        self.config = config
        self.host = config.resolve_host(host)
        self.host_config = config.host_config(host, insecure=insecure)
        self._timeout = timeout
        self._client: Client | AuthenticatedClient | None = None

    @property
    def client(self) -> Client | AuthenticatedClient:
        if self._client is None:
            self._client = build_client(self.host_config, timeout=self._timeout)
        return self._client

    @property
    def is_authenticated(self) -> bool:
        return bool(self.host_config.token)

    def require_auth(self) -> Client | AuthenticatedClient:
        """Fail early with an actionable message instead of letting the API 401."""
        if not self.is_authenticated:
            raise AuthError(
                f"not logged in to {self.host}",
                hint=f"run 'jsonhub auth login --host {self.host}'",
            )
        if self.host_config.is_expired:
            raise AuthError(
                f"credentials for {self.host} have expired",
                hint=f"run 'jsonhub auth login --host {self.host}'",
            )
        return self.client


def _problem_from_body(response: Response[Any]) -> tuple[str | None, str | None]:
    """Pull ``title``/``detail`` out of an error body the SDK could not parse.

    The SDK's ``Response`` exposes raw ``content`` rather than httpx's
    ``.json()``, and only documented status codes get a model, so undocumented
    errors have to be read straight off the bytes.
    """
    try:
        body = json.loads(response.content)
    except (ValueError, TypeError):
        text = response.content.decode(errors="replace").strip()
        return (text[:PROBLEM_TEXT_LIMIT] or None, None)
    if not isinstance(body, dict):
        return (None, None)
    title = body.get("title") or body.get("error") or body.get("message")
    detail = body.get("detail") or body.get("error_description")
    return (str(title) if title else None, str(detail) if detail else None)


def _violations(parsed: ConstraintViolation) -> list[tuple[str, str]]:
    items = unset_to_none(parsed.violations) or []
    return [(v.property_path, v.message) for v in items]


def check(response: Response[Any], *, resource: str = "resource") -> Any:
    """Raise on failure, otherwise return the parsed payload untouched.

    ``resource`` is only used to phrase the 404 message ("entity not found").
    """
    status = response.status_code
    parsed = response.parsed

    if isinstance(parsed, ConstraintViolation):
        message = unset_to_none(parsed.detail) or "the server rejected the payload"
        raise ValidationError(message, violations=_violations(parsed))

    if status < 400:
        return parsed

    if isinstance(parsed, Error):
        title = unset_to_none(parsed.title)
        detail = unset_to_none(parsed.detail)
    else:
        title, detail = _problem_from_body(response)

    if status == HTTPStatus.UNAUTHORIZED:
        raise AuthError(
            detail or title or "authentication required",
            hint="run 'jsonhub auth login' (or check $JSONHUB_TOKEN)",
        )
    if status == HTTPStatus.NOT_FOUND:
        raise NotFoundError(detail or title or f"{resource} not found")
    if status == HTTPStatus.FORBIDDEN:
        raise ApiError(
            detail or title or f"you are not allowed to do that to this {resource}",
            status=status,
        )

    phrase = HTTPStatus(status).phrase if status in {s.value for s in HTTPStatus} else "error"
    raise ApiError(title or detail or f"{phrase} ({status})", status=status, detail=detail if title else None)


def payload(response: Response[Any], *, resource: str = "resource") -> dict[str, Any]:
    """``check`` plus a guarantee that we got a body back, as a plain dict.

    The CLI's internal currency is the API's own JSON, so typed SDK models are
    flattened with ``to_dict()`` right at the boundary. That keeps ``--json``
    output byte-comparable with the HTTP response and lets one set of table
    renderers handle both single resources and collection members.
    """
    parsed = check(response, resource=resource)
    if parsed is None:
        raise ApiError(f"the server returned an empty {resource} body", status=response.status_code)
    if isinstance(parsed, dict):
        return parsed
    to_dict = getattr(parsed, "to_dict", None)
    if callable(to_dict):
        result: dict[str, Any] = to_dict()
        return result
    raise ApiError(f"unexpected {resource} payload of type {type(parsed).__name__}")
