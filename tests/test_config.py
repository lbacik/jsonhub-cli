"""Config file loading, env-var layering, and on-disk permissions."""

from __future__ import annotations

import json
import stat
from pathlib import Path

import pytest

from jsonhub_cli.config import ENV_HOST, ENV_INSECURE, ENV_TOKEN, Config, HostConfig, normalize_host
from jsonhub_cli.errors import ConfigError


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("api.jsonhub.cloud", "api.jsonhub.cloud"),
        ("https://api.jsonhub.cloud", "api.jsonhub.cloud"),
        ("https://API.JsonHub.Cloud/api/", "api.jsonhub.cloud"),
        ("localhost:8080", "localhost:8080"),
    ],
)
def test_normalize_host_collapses_urls_to_one_key(raw: str, expected: str) -> None:
    assert normalize_host(raw) == expected


def test_missing_file_yields_defaults(tmp_path: Path) -> None:
    config = Config.load(tmp_path / "absent.json")
    assert config.hosts == {}
    assert config.default_host == "api.jsonhub.cloud"


def test_malformed_file_is_reported_not_crashed(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    path.write_text("{not json")
    with pytest.raises(ConfigError) as excinfo:
        Config.load(path)
    assert "not valid JSON" in excinfo.value.message


def test_saved_file_is_owner_readable_only(tmp_path: Path) -> None:
    config = Config.load(tmp_path / "nested" / "config.json")
    config.set_host_config("api.test.example", HostConfig(base_url="https://api.test.example", token="secret"))
    config.save()

    mode = stat.S_IMODE(config.path.stat().st_mode)
    assert mode == 0o600, f"config holds a bearer token; got mode {oct(mode)}"
    assert json.loads(config.path.read_text())["hosts"]["api.test.example"]["token"] == "secret"


def test_save_does_not_leave_a_temp_file_behind(tmp_path: Path) -> None:
    config = Config.load(tmp_path / "config.json")
    config.set_host_config("h", HostConfig(base_url="https://h"))
    config.save()
    assert [p.name for p in tmp_path.iterdir()] == ["config.json"]


def test_env_token_overrides_stored_credentials(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = Config.load(tmp_path / "config.json")
    config.set_host_config("api.test.example", HostConfig(base_url="https://api.test.example", token="on-disk"))

    monkeypatch.setenv(ENV_TOKEN, "from-env")
    assert config.host_config("api.test.example").token == "from-env"


def test_env_token_does_not_inherit_an_expired_oauth_deadline(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = Config.load(tmp_path / "config.json")
    config.set_host_config(
        "api.test.example",
        HostConfig(base_url="https://api.test.example", token="old", token_type="oauth", expires_at=1),
    )
    monkeypatch.setenv(ENV_TOKEN, "from-env")

    assert not config.host_config("api.test.example").is_expired


def test_env_token_is_not_written_back_to_disk(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = Config.load(tmp_path / "config.json")
    config.set_host_config("api.test.example", HostConfig(base_url="https://api.test.example", token="on-disk"))
    monkeypatch.setenv(ENV_TOKEN, "from-env")

    config.save()

    assert json.loads(config.path.read_text())["hosts"]["api.test.example"]["token"] == "on-disk"


def test_host_resolution_order(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = Config.load(tmp_path / "config.json")
    config.default_host = "from-config"
    monkeypatch.setenv(ENV_HOST, "from-env")

    assert config.resolve_host("from-flag") == "from-flag"
    assert config.resolve_host(None) == "from-env"
    monkeypatch.delenv(ENV_HOST)
    assert config.resolve_host(None) == "from-config"


def test_unknown_host_still_gives_an_anonymous_entry(tmp_path: Path) -> None:
    config = Config.load(tmp_path / "config.json")
    entry = config.host_config("brand.new.example")
    assert entry.token is None
    assert entry.base_url == "https://brand.new.example"


def test_localhost_defaults_to_plain_http(tmp_path: Path) -> None:
    config = Config.load(tmp_path / "config.json")
    assert config.host_config("localhost:8000").base_url == "http://localhost:8000"


def test_anonymous_drops_credentials_but_keeps_the_endpoint() -> None:
    entry = HostConfig(
        base_url="https://api.test.example",
        token="secret",
        token_type="oauth",
        refresh_token="refresh",
        expires_at=1770000000,
        client_id="cli",
        scope="mcp",
    )

    anon = entry.anonymous()

    assert (anon.token, anon.token_type, anon.refresh_token, anon.expires_at, anon.scope) == (None,) * 5
    assert anon.base_url == entry.base_url
    # client_id is a public identifier, not a credential: the flow reuses it.
    assert anon.client_id == "cli"
    assert entry.token == "secret", "the original entry must not be mutated"


def test_anonymous_keeps_the_transport_settings() -> None:
    entry = HostConfig(base_url="https://api.test.example", token="secret", insecure=True)

    # The OAuth flow has to reach the same endpoint on the same terms.
    assert entry.anonymous().insecure is True


def test_a_default_setting_is_not_written_to_disk() -> None:
    assert HostConfig(base_url="https://api.test.example").to_dict() == {"base_url": "https://api.test.example"}


def test_a_non_boolean_insecure_in_the_file_does_not_disable_verification() -> None:
    # Only a literal JSON true may turn verification off; "false" is a string.
    assert HostConfig.from_dict("h", {"base_url": "https://h", "insecure": "false"}).insecure is False


def test_env_insecure_overrides_the_stored_setting(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = Config.load(tmp_path / "config.json")
    config.set_host_config("api.test.example", HostConfig(base_url="https://api.test.example", insecure=True))

    monkeypatch.setenv(ENV_INSECURE, "no")
    assert config.host_config("api.test.example").insecure is False


def test_env_insecure_is_not_written_back_to_disk(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = Config.load(tmp_path / "config.json")
    config.set_host_config("api.test.example", HostConfig(base_url="https://api.test.example"))
    monkeypatch.setenv(ENV_INSECURE, "yes")

    config.save()

    assert "insecure" not in json.loads(config.path.read_text())["hosts"]["api.test.example"]


def test_env_insecure_rejects_a_value_that_is_not_a_boolean(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = Config.load(tmp_path / "config.json")
    monkeypatch.setenv(ENV_INSECURE, "sometimes")

    with pytest.raises(ConfigError):
        config.host_config("api.test.example")


def test_the_insecure_argument_beats_both(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = Config.load(tmp_path / "config.json")
    monkeypatch.setenv(ENV_INSECURE, "off")

    assert config.host_config("api.test.example", insecure=True).insecure is True


def test_oauth_expiry_uses_a_safety_margin() -> None:
    import time

    assert HostConfig(base_url="x", expires_at=int(time.time()) + 10).is_expired
    assert not HostConfig(base_url="x", expires_at=int(time.time()) + 3600).is_expired
    # A personal access token has no local expiry; only the server can say.
    assert not HostConfig(base_url="x", token="pat", token_type="pat").is_expired
