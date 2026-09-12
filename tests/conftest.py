"""Shared fixtures.

Every test runs against a mocked httpx transport (``pytest-httpx``) and an
isolated config directory, so nothing here touches the network or the
developer's real ``~/.config/jsonhub``.
"""

from __future__ import annotations

import io
import json
import sys
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from jsonhub_cli import api
from jsonhub_cli.config import ENV_CONFIG_DIR, ENV_HOST, ENV_INSECURE, ENV_TOKEN, HostConfig
from jsonhub_cli.main import run

BASE_URL = "https://api.test.example"
HOST = "api.test.example"
TOKEN = "test-token-abcdef"


@pytest.fixture(autouse=True)
def isolated_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Point the CLI at a throwaway config dir and clear inherited env vars."""
    config_dir = tmp_path / "config"
    monkeypatch.setenv(ENV_CONFIG_DIR, str(config_dir))
    monkeypatch.delenv(ENV_TOKEN, raising=False)
    monkeypatch.delenv(ENV_INSECURE, raising=False)
    monkeypatch.setenv(ENV_HOST, HOST)
    yield config_dir


@pytest.fixture
def logged_in(isolated_env: Path) -> Path:
    """Write a config file holding a personal access token for ``HOST``."""
    isolated_env.mkdir(parents=True, exist_ok=True)
    path = isolated_env / "config.json"
    path.write_text(
        json.dumps(
            {
                "default_host": HOST,
                "hosts": {HOST: {"base_url": BASE_URL, "token": TOKEN, "token_type": "pat"}},
            }
        )
    )
    return path


@pytest.fixture
def transport_settings(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Record the settings every SDK client in a run is built from.

    ``api._client_kwargs`` is the one place a per-host transport setting is
    honoured, and a mocked transport cannot show whether TLS verification was
    on, so this is where the tests look instead.
    """
    original = api._client_kwargs
    seen: list[dict[str, Any]] = []

    def spy(cfg: HostConfig, timeout: float) -> dict[str, Any]:
        kwargs = original(cfg, timeout)
        seen.append(kwargs)
        return kwargs

    monkeypatch.setattr(api, "_client_kwargs", spy)
    return seen


@dataclass(frozen=True)
class Result:
    """What a CLI invocation produced."""

    exit_code: int
    stdout: str
    stderr: str


@pytest.fixture
def invoke(capsys: pytest.CaptureFixture[str]) -> Callable[..., Result]:
    """Run the CLI exactly as the installed ``jsonhub`` script does.

    Going through :func:`jsonhub_cli.main.run` rather than Typer's ``CliRunner``
    means the tests exercise the real error boundary -- the place where the
    CLI's exceptions become messages and exit codes.
    """

    def _invoke(*args: str, stdin: str | None = None) -> Result:
        if stdin is not None:
            sys.stdin = io.StringIO(stdin)
        try:
            code = run(list(args))
        finally:
            if stdin is not None:
                sys.stdin = sys.__stdin__
        captured = capsys.readouterr()
        return Result(exit_code=code, stdout=captured.out, stderr=captured.err)

    return _invoke


def hal_collection(*items: dict[str, Any], total: int | None = None, next_page: bool = False) -> dict[str, Any]:
    """Build a HAL collection body the way JsonHub does."""
    links: dict[str, Any] = {"self": {"href": "/api/entities?page=1"}}
    if next_page:
        links["next"] = {"href": "/api/entities?page=2"}
    body: dict[str, Any] = {
        "_links": links,
        "totalItems": total if total is not None else len(items),
        "itemsPerPage": len(items),
    }
    if items:
        body["_embedded"] = {"item": list(items)}
    return body


def entity(
    entity_id: str = "018baea0-f999-73f4-9eb4-d0c62f3ac49b",
    slug: str = "testing",
    data: dict[str, Any] | None = None,
    definition_slug: str = "base-v1",
    private: bool = False,
) -> dict[str, Any]:
    related_definition = {"id": "d0000000-0000-0000-0000-000000000001", "slug": definition_slug}
    return {
        "_links": {"self": {"href": f"/api/entities/{entity_id}"}},
        "_embedded": {"definition": dict(related_definition)},
        "id": entity_id,
        "slug": slug,
        "data": data if data is not None else {"name": "foo"},
        "definition": related_definition,
        "private": private,
        "isOwnedByCurrentUser": True,
    }


def definition(
    definition_id: str = "d0000000-0000-0000-0000-000000000001",
    slug: str = "base-v1",
    schema: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "_links": {"self": {"href": f"/api/definitions/{definition_id}"}},
        "id": definition_id,
        "slug": slug,
        "jsonSchema": schema if schema is not None else {"type": "object", "properties": {"name": {"type": "string"}}},
        "isOwnedByCurrentUser": True,
    }


USER_ID = "0193d9a1-4c2f-7b6e-8f10-2a5c9d3e7b41"
USER_EMAIL = "ada@example.test"


def quota(entities: int = 3, private: int = 0, definitions: int = 1) -> dict[str, Any]:
    """A ``/api/users/me`` body with everything the schema requires."""
    return {
        "id": USER_ID,
        "email": USER_EMAIL,
        "limits": {
            "entities": {"used": entities, "limit": 100},
            "privateEntities": {"used": private, "limit": 10},
            "definitions": {"used": definitions, "limit": 20},
        },
    }
