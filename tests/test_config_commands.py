"""End-to-end tests for ``jsonhub config`` and the root options."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pytest_httpx import HTTPXMock

from jsonhub_cli import __version__
from jsonhub_cli.config import Config

from .conftest import HOST, TOKEN, entity, hal_collection


def test_version_prints_the_package_version(invoke: Any) -> None:
    result = invoke("--version")

    assert result.exit_code == 0
    assert __version__ in result.stdout


def test_config_list_never_prints_a_token(invoke: Any, logged_in: Path) -> None:
    result = invoke("config", "list")

    assert result.exit_code == 0
    assert TOKEN not in result.stdout
    assert HOST in result.stdout


def test_config_list_json_reports_login_state_only(invoke: Any, logged_in: Path) -> None:
    result = invoke("config", "list", "--json")

    document = json.loads(result.stdout)
    assert document["hosts"][HOST] == {
        "base_url": "https://api.test.example",
        "insecure": False,
        "logged_in": True,
        "scope": None,
    }


def test_set_host_registers_a_deployment(invoke: Any, isolated_env: Path) -> None:
    result = invoke("config", "set-host", "jsonhub.internal", "--base-url", "https://api.jsonhub.internal")

    assert result.exit_code == 0
    stored = Config.load(isolated_env / "config.json")
    assert stored.hosts["jsonhub.internal"].base_url == "https://api.jsonhub.internal"


def test_set_host_defaults_the_base_url_to_the_hostname(invoke: Any, isolated_env: Path) -> None:
    invoke("config", "set-host", "jsonhub.internal")

    stored = Config.load(isolated_env / "config.json")
    assert stored.hosts["jsonhub.internal"].base_url == "https://jsonhub.internal"


def test_the_first_host_becomes_the_default(invoke: Any, isolated_env: Path) -> None:
    invoke("config", "set-host", "jsonhub.internal")

    assert Config.load(isolated_env / "config.json").default_host == "jsonhub.internal"


def test_set_default_refuses_an_unconfigured_host(invoke: Any) -> None:
    result = invoke("config", "set-default", "never.configured")

    assert result.exit_code == 1
    assert "not configured" in result.stderr


def test_set_default_switches_hosts(invoke: Any, isolated_env: Path) -> None:
    invoke("config", "set-host", "one.example")
    invoke("config", "set-host", "two.example")
    invoke("config", "set-default", "two.example")

    assert Config.load(isolated_env / "config.json").default_host == "two.example"


def test_host_flag_redirects_the_request(httpx_mock: HTTPXMock, invoke: Any) -> None:
    httpx_mock.add_response(url="https://other.example/api/entities?page=1&limit=30", json=hal_collection(entity()))

    result = invoke("--host", "other.example", "entity", "list")

    assert result.exit_code == 0
    assert httpx_mock.get_requests()[0].url.host == "other.example"


def test_env_token_authenticates_without_a_config_file(httpx_mock: HTTPXMock, invoke: Any, monkeypatch: Any) -> None:
    monkeypatch.setenv("JSONHUB_TOKEN", "from-the-environment")
    httpx_mock.add_response(json=entity(), status_code=201)

    result = invoke("entity", "create", "-d", "{}")

    assert result.exit_code == 0
    assert httpx_mock.get_requests()[0].headers["Authorization"] == "Bearer from-the-environment"


def test_an_unknown_command_is_a_usage_error(invoke: Any) -> None:
    result = invoke("entity", "frobnicate")

    assert result.exit_code == 2
