"""Profile configuration: TOML loading, validation and lookup (ADR-003, ADR-004).

The configuration is the only place credentials live. Two rules shape this module:

* **Fail fast and readably** (NFR-08) — a typo in the file must produce a message that says
  which profile and which field, not a Pydantic traceback.
* **Never leak a secret** (FR-08) — passwords are `SecretStr`, so they cannot appear in a
  `repr`, a log line or a serialised model by accident.
"""

from __future__ import annotations

import os
import tomllib
from pathlib import Path
from typing import Annotated, Self

from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError, model_validator

from untis_mcp.errors import ConfigError, ProfileNotFound

CONFIG_PATH_ENV = "UNTIS_MCP_CONFIG"
DEFAULT_CONFIG_PATH = Path("config/profiles.toml")

# Profile names are addressed by an AI assistant, so they stay short and typeable. Case is
# preserved but lookup is case-insensitive (see Config.get).
ProfileName = Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]*$", max_length=40)]


class ProfileConfig(BaseModel):
    """Credentials and connection details for one child at one school."""

    model_config = ConfigDict(extra="forbid")

    name: ProfileName
    description: str = ""
    server: str
    school: str
    username: str
    password: SecretStr | None = None
    password_env: str | None = None
    timezone: str = "Europe/Berlin"
    # Only needed when one login can see several children (FR-11); otherwise the single
    # child the login reports is used.
    student_id: int | None = None

    @model_validator(mode="after")
    def _check_server(self) -> Self:
        server = self.server.strip().rstrip("/")
        if not server.startswith(("http://", "https://")):
            raise ValueError(
                f"server must start with https:// (got {self.server!r}); use the base URL of "
                f"the WebUntis server, e.g. https://ajax.webuntis.com"
            )
        self.server = server
        return self

    @model_validator(mode="after")
    def _resolve_password(self) -> Self:
        """Resolve `password_env` at load time, so a missing secret fails at startup."""
        if self.password is not None and self.password_env is not None:
            raise ValueError("set either password or password_env, not both")
        if self.password_env is not None:
            value = os.environ.get(self.password_env)
            if not value:
                raise ValueError(
                    f"password_env names {self.password_env!r}, but that environment "
                    f"variable is unset or empty"
                )
            self.password = SecretStr(value)
        if self.password is None:
            raise ValueError("neither password nor password_env is set")
        return self

    @property
    def secret(self) -> str:
        """The plaintext password. Only the client layer may call this."""
        assert self.password is not None  # guaranteed by _resolve_password
        return self.password.get_secret_value()


class Config(BaseModel):
    """The whole configuration file: one or more profiles."""

    model_config = ConfigDict(extra="forbid")

    profiles: list[ProfileConfig] = Field(min_length=1)

    @model_validator(mode="after")
    def _unique_names(self) -> Self:
        seen: dict[str, str] = {}
        for profile in self.profiles:
            key = profile.name.casefold()
            if key in seen:
                raise ValueError(
                    f"duplicate profile name {profile.name!r} (already used as "
                    f"{seen[key]!r}); names must be unique, ignoring case"
                )
            seen[key] = profile.name
        return self

    @property
    def names(self) -> list[str]:
        return [profile.name for profile in self.profiles]

    def get(self, name: str) -> ProfileConfig:
        """Look up a profile by name, case-insensitively (FR-03, FR-04)."""
        wanted = name.strip().casefold()
        for profile in self.profiles:
            if profile.name.casefold() == wanted:
                return profile
        raise ProfileNotFound(name, self.names)


def default_config_path() -> Path:
    """The configured path: `$UNTIS_MCP_CONFIG`, else `config/profiles.toml`."""
    configured = os.environ.get(CONFIG_PATH_ENV)
    return Path(configured).expanduser() if configured else DEFAULT_CONFIG_PATH


def load_config(path: Path | None = None) -> Config:
    """Read and validate the configuration, or raise `ConfigError` with a readable message."""
    path = path or default_config_path()

    if not path.is_file():
        raise ConfigError(
            f"configuration file not found: {path}\n"
            f"copy config/profiles.example.toml to that path and fill in real credentials, "
            f"or point {CONFIG_PATH_ENV} somewhere else"
        )

    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path} is not valid TOML: {exc}") from exc
    except OSError as exc:
        raise ConfigError(f"cannot read {path}: {exc}") from exc

    try:
        return Config.model_validate(raw)
    except ValidationError as exc:
        raise ConfigError(f"{path} is invalid:\n{_render(exc)}") from exc


def _render(error: ValidationError) -> str:
    """Turn Pydantic's error list into lines an operator can act on."""
    lines = []
    for item in error.errors():
        location = ".".join(str(part) for part in item["loc"]) or "(root)"
        lines.append(f"  - {location}: {item['msg']}")
    return "\n".join(lines)
