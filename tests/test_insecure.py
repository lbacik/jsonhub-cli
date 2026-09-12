"""Talking to a deployment whose TLS certificate is not signed by a trusted CA.

Covers the whole path of the setting: the ``--insecure`` flag, the stored
per-host setting, the environment variable layered on top, and the warning that
makes it impossible to leave verification off without noticing.
"""

from __future__ import annotations

import json
import ssl
from pathlib import Path
from typing import Any

import httpx
import pytest
from pytest_httpx import HTTPXMock

from jsonhub_cli.config import ENV_INSECURE, Config

from .conftest import BASE_URL, HOST, entity, hal_collection, quota


@pytest.fixture
def insecure_host(isolated_env: Path) -> Path:
    """A config file whose only host has certificate verification disabled."""
    isolated_env.mkdir(parents=True, exist_ok=True)
    path = isolated_env / "config.json"
    path.write_text(json.dumps({"default_host": HOST, "hosts": {HOST: {"base_url": BASE_URL, "insecure": True}}}))
    return path


def _list_entities(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(url=f"{BASE_URL}/api/entities?page=1&limit=30", json=hal_collection(entity()))


def _certificate_failure() -> httpx.ConnectError:
    request = httpx.Request("GET", f"{BASE_URL}/api/entities?page=1&limit=30")
    certificate_error = ssl.SSLCertVerificationError(
        "[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: self signed certificate"
    )
    transport_error = httpx.ConnectError(str(certificate_error), request=request)
    transport_error.__cause__ = certificate_error
    return transport_error


def test_certificate_failure_explains_how_to_trust_the_host(
    httpx_mock: HTTPXMock, invoke: Any, transport_settings: Any
) -> None:
    httpx_mock.add_exception(_certificate_failure())
    _list_entities(httpx_mock)

    result = invoke("entity", "list", "--json")
    stderr = " ".join(result.stderr.split())

    assert result.exit_code == 1
    assert result.stdout == ""
    assert "could not verify the TLS certificate" in stderr
    assert HOST in stderr
    assert "CERTIFICATE_VERIFY_FAILED" in stderr
    assert "--insecure" in stderr
    assert f"jsonhub config set-host {HOST} --insecure" in stderr
    assert "could not reach the API" not in stderr

    retry = invoke("--insecure", "entity", "list", "--json")

    assert retry.exit_code == 0
    assert json.loads(retry.stdout) == [entity()]
    assert [settings["verify_ssl"] for settings in transport_settings] == [True, False]


def test_non_tls_transport_failure_keeps_the_network_message(httpx_mock: HTTPXMock, invoke: Any) -> None:
    request = httpx.Request("GET", f"{BASE_URL}/api/entities?page=1&limit=30")
    httpx_mock.add_exception(httpx.ConnectError("Connection refused", request=request))

    result = invoke("entity", "list", "--json")
    stderr = " ".join(result.stderr.split())

    assert result.exit_code == 1
    assert result.stdout == ""
    assert "could not reach the API: Connection refused" in stderr
    assert "check --host and your network connection" in stderr
    assert "could not verify the TLS certificate" not in stderr
    assert "--insecure" not in stderr


def test_verification_is_on_by_default(httpx_mock: HTTPXMock, invoke: Any, transport_settings: Any) -> None:
    _list_entities(httpx_mock)

    result = invoke("entity", "list")

    assert result.exit_code == 0
    assert [settings["verify_ssl"] for settings in transport_settings] == [True]


def test_insecure_flag_reaches_the_transport(httpx_mock: HTTPXMock, invoke: Any, transport_settings: Any) -> None:
    _list_entities(httpx_mock)

    result = invoke("--insecure", "entity", "list")

    assert result.exit_code == 0
    assert [settings["verify_ssl"] for settings in transport_settings] == [False]


def test_stored_setting_reaches_the_transport(
    httpx_mock: HTTPXMock, invoke: Any, transport_settings: Any, insecure_host: Path
) -> None:
    _list_entities(httpx_mock)

    result = invoke("entity", "list")

    assert result.exit_code == 0
    assert [settings["verify_ssl"] for settings in transport_settings] == [False]


def test_env_var_disables_verification_for_a_plain_host(
    httpx_mock: HTTPXMock, invoke: Any, transport_settings: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(ENV_INSECURE, "1")
    _list_entities(httpx_mock)

    invoke("entity", "list")

    assert [settings["verify_ssl"] for settings in transport_settings] == [False]


def test_env_var_overrides_the_stored_setting(
    httpx_mock: HTTPXMock,
    invoke: Any,
    transport_settings: Any,
    insecure_host: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(ENV_INSECURE, "false")
    _list_entities(httpx_mock)

    invoke("entity", "list")

    assert [settings["verify_ssl"] for settings in transport_settings] == [True]


def test_the_flag_wins_over_the_env_var(
    httpx_mock: HTTPXMock, invoke: Any, transport_settings: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(ENV_INSECURE, "0")
    _list_entities(httpx_mock)

    invoke("--insecure", "entity", "list")

    assert [settings["verify_ssl"] for settings in transport_settings] == [False]


def test_an_unreadable_env_var_is_a_config_error(invoke: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ENV_INSECURE, "maybe")

    result = invoke("entity", "list")

    assert result.exit_code == 1
    assert ENV_INSECURE in result.stderr


def test_the_warning_names_the_host_on_stderr(httpx_mock: HTTPXMock, invoke: Any) -> None:
    _list_entities(httpx_mock)

    result = invoke("--insecure", "entity", "list")

    assert HOST in result.stderr
    assert "verification is disabled" in result.stderr


def test_the_warning_never_touches_a_json_document(httpx_mock: HTTPXMock, invoke: Any) -> None:
    _list_entities(httpx_mock)

    result = invoke("--insecure", "entity", "list", "--json")

    assert "verification is disabled" in result.stderr
    # --json promises exactly one JSON document on stdout, warning or not.
    assert json.loads(result.stdout) == [entity()]


def test_a_stored_insecure_host_warns_without_the_flag(httpx_mock: HTTPXMock, invoke: Any, insecure_host: Path) -> None:
    _list_entities(httpx_mock)

    result = invoke("entity", "list")

    assert "verification is disabled" in result.stderr


def test_login_stores_the_flag_and_status_uses_it_afterwards(
    httpx_mock: HTTPXMock, invoke: Any, transport_settings: Any, isolated_env: Path
) -> None:
    httpx_mock.add_response(url=f"{BASE_URL}/api/users/me", json=quota())
    httpx_mock.add_response(url=f"{BASE_URL}/api/users/me", json=quota())

    result = invoke("--insecure", "auth", "login", "--with-token", "--base-url", BASE_URL, stdin="pat-secret\n")

    assert result.exit_code == 0, result.stderr
    assert json.loads((isolated_env / "config.json").read_text())["hosts"][HOST]["insecure"] is True

    result = invoke("auth", "status")

    assert result.exit_code == 0, result.stderr
    assert "logged in as" in result.stdout
    assert transport_settings and all(settings["verify_ssl"] is False for settings in transport_settings)


def test_login_uses_and_keeps_a_stored_setting(
    httpx_mock: HTTPXMock, invoke: Any, transport_settings: Any, insecure_host: Path
) -> None:
    httpx_mock.add_response(url=f"{BASE_URL}/api/users/me", json=quota())

    result = invoke("auth", "login", "--with-token", stdin="pat-secret\n")

    assert result.exit_code == 0, result.stderr
    assert transport_settings and all(settings["verify_ssl"] is False for settings in transport_settings)
    assert json.loads(insecure_host.read_text())["hosts"][HOST]["insecure"] is True


def test_set_host_stores_the_setting(invoke: Any, isolated_env: Path) -> None:
    result = invoke("config", "set-host", "jsonhub.internal", "--insecure")

    assert result.exit_code == 0
    stored = Config.load(isolated_env / "config.json")
    assert stored.hosts["jsonhub.internal"].insecure is True


def test_set_host_can_turn_verification_back_on(invoke: Any, isolated_env: Path) -> None:
    invoke("config", "set-host", "jsonhub.internal", "--insecure")

    invoke("config", "set-host", "jsonhub.internal", "--no-insecure")

    stored = Config.load(isolated_env / "config.json")
    assert stored.hosts["jsonhub.internal"].insecure is False


def test_set_host_leaves_the_setting_alone_when_not_asked(invoke: Any, isolated_env: Path) -> None:
    invoke("config", "set-host", "jsonhub.internal", "--insecure")

    invoke("config", "set-host", "jsonhub.internal", "--base-url", "https://elsewhere.internal")

    stored = Config.load(isolated_env / "config.json")
    assert stored.hosts["jsonhub.internal"].insecure is True


def test_a_host_that_verifies_leaves_no_trace_in_the_file(invoke: Any, isolated_env: Path) -> None:
    invoke("config", "set-host", "jsonhub.internal")

    raw = json.loads((isolated_env / "config.json").read_text())
    assert "insecure" not in raw["hosts"]["jsonhub.internal"]


def test_config_list_reports_which_hosts_skip_verification(invoke: Any, insecure_host: Path) -> None:
    result = invoke("config", "list", "--json")

    assert json.loads(result.stdout)["hosts"][HOST]["insecure"] is True


def test_config_list_table_reports_the_setting(invoke: Any, insecure_host: Path) -> None:
    result = invoke("config", "list")

    assert result.exit_code == 0
    # Piped output is TSV: the host's row carries the flag as a machine value.
    assert "true" in result.stdout.splitlines()[-1].split("\t")
