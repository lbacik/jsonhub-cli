"""Persistent CLI configuration and credential storage.

Layout of ``~/.config/jsonhub/config.json``::

    {
      "default_host": "api.jsonhub.cloud",
      "hosts": {
        "api.jsonhub.cloud": {
          "base_url": "https://api.jsonhub.cloud",
          "token": "...",
          "token_type": "oauth",
          "refresh_token": null,
          "expires_at": 1770000000,
          "client_id": "...",
          "scope": "mcp"
        }
      }
    }

The file holds bearer tokens, so it is written ``0600`` inside a ``0700``
directory and replaced atomically.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .errors import ConfigError

DEFAULT_HOST = "api.jsonhub.cloud"
DEFAULT_BASE_URL = f"https://{DEFAULT_HOST}"

ENV_HOST = "JSONHUB_HOST"
ENV_TOKEN = "JSONHUB_TOKEN"
ENV_CONFIG_DIR = "JSONHUB_CONFIG_DIR"

# Refresh/re-login slightly before the real deadline so a long-running command
# does not die halfway through with a 401.
EXPIRY_SKEW_SECONDS = 60


def config_dir() -> Path:
    """Directory holding ``config.json``, honouring ``JSONHUB_CONFIG_DIR``."""
    override = os.environ.get(ENV_CONFIG_DIR)
    if override:
        return Path(override).expanduser()
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg).expanduser() if xdg else Path.home() / ".config"
    return base / "jsonhub"


def config_path() -> Path:
    return config_dir() / "config.json"


def normalize_host(value: str) -> str:
    """Reduce a user-supplied host or URL to a bare hostname key.

    ``https://api.jsonhub.cloud/api/`` and ``api.jsonhub.cloud`` are the same
    host, so both must land on the same credential entry.
    """
    value = value.strip()
    if not value:
        raise ConfigError("host must not be empty")
    parsed = urlparse(value if "//" in value else f"//{value}")
    host = parsed.netloc or parsed.path
    return host.strip("/").lower()


def base_url_for(host: str) -> str:
    """Best guess at the API root for a host lacking an explicit base URL."""
    scheme = "http" if host.startswith(("localhost", "127.0.0.1")) else "https"
    return f"{scheme}://{host}"


@dataclass
class HostConfig:
    """Credentials and endpoint settings for a single JsonHub deployment."""

    base_url: str
    token: str | None = None
    token_type: str | None = None  # "oauth" | "pat"
    refresh_token: str | None = None
    expires_at: int | None = None
    client_id: str | None = None
    scope: str | None = None

    @property
    def is_expired(self) -> bool:
        """True when an OAuth token is past (or nearly past) its lifetime.

        Personal access tokens carry no ``expires_at`` locally, so they are
        never treated as expired here -- the server decides.
        """
        if self.expires_at is None:
            return False
        return time.time() >= self.expires_at - EXPIRY_SKEW_SECONDS

    @classmethod
    def from_dict(cls, host: str, raw: dict[str, Any]) -> HostConfig:
        known = {f for f in cls.__dataclass_fields__}
        data = {k: v for k, v in raw.items() if k in known}
        data.setdefault("base_url", base_url_for(host))
        return cls(**data)

    def to_dict(self) -> dict[str, Any]:
        return {k: v for k, v in asdict(self).items() if v is not None}


@dataclass
class Config:
    """The whole config file, plus the environment overrides layered on top."""

    default_host: str = DEFAULT_HOST
    hosts: dict[str, HostConfig] = field(default_factory=dict)
    path: Path = field(default_factory=config_path)

    @classmethod
    def load(cls, path: Path | None = None) -> Config:
        path = path or config_path()
        if not path.exists():
            return cls(path=path)
        try:
            raw = json.loads(path.read_text())
        except json.JSONDecodeError as exc:
            raise ConfigError(
                f"{path} is not valid JSON: {exc}",
                hint="fix or delete the file, then run 'jsonhub auth login' again",
            ) from exc
        except OSError as exc:
            raise ConfigError(f"cannot read {path}: {exc}") from exc
        if not isinstance(raw, dict):
            raise ConfigError(f"{path} must contain a JSON object")
        hosts = {
            normalize_host(h): HostConfig.from_dict(normalize_host(h), v)
            for h, v in (raw.get("hosts") or {}).items()
            if isinstance(v, dict)
        }
        return cls(
            default_host=normalize_host(raw.get("default_host") or DEFAULT_HOST),
            hosts=hosts,
            path=path,
        )

    def save(self) -> None:
        """Atomically rewrite the config file with owner-only permissions."""
        payload = {
            "default_host": self.default_host,
            "hosts": {h: c.to_dict() for h, c in sorted(self.hosts.items())},
        }
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        tmp = self.path.with_name(f"{self.path.name}.tmp")
        # Create the temp file 0600 up front: it holds the token even before the
        # rename, so it must never be briefly world-readable.
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            with os.fdopen(fd, "w") as fh:
                json.dump(payload, fh, indent=2, sort_keys=True)
                fh.write("\n")
            os.replace(tmp, self.path)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise

    def resolve_host(self, host: str | None = None) -> str:
        """Pick the host to talk to: ``--host``, then ``$JSONHUB_HOST``, then config."""
        candidate = host or os.environ.get(ENV_HOST) or self.default_host
        return normalize_host(candidate)

    def host_config(self, host: str | None = None) -> HostConfig:
        """Return the config for a host, with ``$JSONHUB_TOKEN`` layered on top.

        Always returns an object -- an unknown host yields an unauthenticated
        entry so read-only commands still work against public data.
        """
        key = self.resolve_host(host)
        cfg = self.hosts.get(key) or HostConfig(base_url=base_url_for(key))
        env_token = os.environ.get(ENV_TOKEN)
        if env_token:
            # Environment wins, and is deliberately not persisted.
            cfg = HostConfig(
                base_url=cfg.base_url,
                token=env_token,
                token_type="pat",
                client_id=cfg.client_id,
                scope=cfg.scope,
            )
        return cfg

    def set_host_config(self, host: str, cfg: HostConfig) -> None:
        self.hosts[normalize_host(host)] = cfg

    def remove_host(self, host: str) -> bool:
        return self.hosts.pop(normalize_host(host), None) is not None
