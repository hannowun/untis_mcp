"""Configuration loading, validation and profile lookup."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from untis_mcp.config import Config, load_config
from untis_mcp.errors import ConfigError, ProfileNotFound

TWO_PROFILES = """
[[profiles]]
name        = "anna"
description = "Anna, Gymnasium Musterstadt"
server      = "https://ajax.webuntis.com"
school      = "gym-musterstadt"
username    = "anna.mustermann"
password    = "secret"

[[profiles]]
name         = "Ben"
description  = "Ben, Realschule Beispieldorf"
server       = "https://herakles.webuntis.com/"
school       = "rs-beispieldorf"
username     = "ben.mustermann"
password_env = "UNTIS_BEN_PASSWORD"
"""


def write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "profiles.toml"
    path.write_text(text, encoding="utf-8")
    return path


# ------------------------------------------------------------------------------- loading


def test_loads_both_password_styles(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("UNTIS_BEN_PASSWORD", "from-env")

    config = load_config(write(tmp_path, TWO_PROFILES))

    assert config.names == ["anna", "Ben"]
    assert config.get("anna").secret == "secret"
    assert config.get("Ben").secret == "from-env"
    # A trailing slash on the server URL would produce "//WebUntis/..." in every request.
    assert config.get("Ben").server == "https://herakles.webuntis.com"


def test_config_path_comes_from_the_environment(tmp_path: Path, monkeypatch):
    path = write(tmp_path, TWO_PROFILES)
    monkeypatch.setenv("UNTIS_BEN_PASSWORD", "from-env")
    monkeypatch.setenv("UNTIS_MCP_CONFIG", str(path))

    assert load_config().names == ["anna", "Ben"]


# ------------------------------------------------------------- failing fast and readably


def test_missing_file_explains_what_to_do(tmp_path: Path):
    with pytest.raises(ConfigError) as excinfo:
        load_config(tmp_path / "nope.toml")

    message = str(excinfo.value)
    assert "not found" in message
    assert "profiles.example.toml" in message


def test_broken_toml_names_the_file(tmp_path: Path):
    with pytest.raises(ConfigError, match="not valid TOML"):
        load_config(write(tmp_path, "[[profiles]\nname = 'anna'"))


def test_missing_field_is_reported_with_its_location(tmp_path: Path):
    text = """
[[profiles]]
name     = "anna"
server   = "https://ajax.webuntis.com"
password = "secret"
"""
    with pytest.raises(ConfigError) as excinfo:
        load_config(write(tmp_path, text))

    message = str(excinfo.value)
    assert "profiles.0.school" in message
    assert "profiles.0.username" in message


def test_unknown_field_is_rejected_rather_than_silently_ignored(tmp_path: Path):
    """A typo like `passwort` must not leave the profile mysteriously password-less."""
    text = """
[[profiles]]
name     = "anna"
server   = "https://ajax.webuntis.com"
school   = "gym"
username = "anna"
passwort = "secret"
"""
    with pytest.raises(ConfigError) as excinfo:
        load_config(write(tmp_path, text))

    assert "passwort" in str(excinfo.value)


def test_missing_password_env_fails_at_startup_not_at_first_request(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("UNTIS_MISSING", raising=False)
    text = """
[[profiles]]
name         = "anna"
server       = "https://ajax.webuntis.com"
school       = "gym"
username     = "anna"
password_env = "UNTIS_MISSING"
"""
    with pytest.raises(ConfigError, match="UNTIS_MISSING"):
        load_config(write(tmp_path, text))


def test_both_password_forms_at_once_is_an_error(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("UNTIS_X", "x")
    text = """
[[profiles]]
name         = "anna"
server       = "https://ajax.webuntis.com"
school       = "gym"
username     = "anna"
password     = "inline"
password_env = "UNTIS_X"
"""
    with pytest.raises(ConfigError, match="not both"):
        load_config(write(tmp_path, text))


def test_server_must_be_a_base_url(tmp_path: Path):
    text = """
[[profiles]]
name     = "anna"
server   = "ajax.webuntis.com"
school   = "gym"
username = "anna"
password = "secret"
"""
    with pytest.raises(ConfigError, match="https://"):
        load_config(write(tmp_path, text))


def test_duplicate_names_are_rejected(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("UNTIS_BEN_PASSWORD", "x")
    text = TWO_PROFILES.replace('name        = "anna"', 'name        = "Ben"')
    with pytest.raises(ConfigError, match="duplicate profile name"):
        load_config(write(tmp_path, text))


def test_an_empty_profile_list_is_rejected(tmp_path: Path):
    with pytest.raises(ConfigError):
        load_config(write(tmp_path, "profiles = []"))


# -------------------------------------------------------------------------------- lookup


def test_lookup_is_case_insensitive_and_tolerates_whitespace(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("UNTIS_BEN_PASSWORD", "x")
    config = load_config(write(tmp_path, TWO_PROFILES))

    assert config.get("ben").name == "Ben"
    assert config.get(" ANNA ").name == "anna"


def test_unknown_profile_lists_the_valid_names(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("UNTIS_BEN_PASSWORD", "x")
    config = load_config(write(tmp_path, TWO_PROFILES))

    with pytest.raises(ProfileNotFound) as excinfo:
        config.get("charlie")

    message = str(excinfo.value)
    assert "'charlie'" in message
    assert "'anna'" in message and "'Ben'" in message


# ------------------------------------------------------------------------------- secrets


def test_passwords_never_appear_in_reprs_dumps_or_logs(tmp_path: Path, monkeypatch, caplog):
    """FR-08: the password must be unreachable except through the explicit accessor."""
    inline = "hunter2-inline-plaintext"
    from_env = "hunter2-env-plaintext"
    monkeypatch.setenv("UNTIS_BEN_PASSWORD", from_env)
    config = load_config(write(tmp_path, TWO_PROFILES.replace('"secret"', f'"{inline}"')))

    rendered = [
        repr(config),
        str(config),
        str(config.model_dump()),
        config.model_dump_json(),
        repr(config.get("anna").password),
    ]
    with caplog.at_level(logging.DEBUG):
        logging.getLogger("test").info("profile: %s", config.get("anna"))
        logging.getLogger("test").info("profiles: %r", config.profiles)
        rendered.append(caplog.text)

    for text in rendered:
        assert inline not in text, f"inline password leaked into: {text[:200]}"
        assert from_env not in text, f"password_env value leaked into: {text[:200]}"

    # ... while the client layer can still get at it deliberately.
    assert config.get("anna").secret == inline
    assert config.get("Ben").secret == from_env


def test_config_model_can_be_built_directly_for_tests():
    """Later layers construct Config in tests; that path must not need a file."""
    config = Config.model_validate(
        {
            "profiles": [
                {
                    "name": "anna",
                    "server": "https://ajax.webuntis.com",
                    "school": "gym",
                    "username": "anna",
                    "password": "secret",
                }
            ]
        }
    )
    assert config.get("anna").timezone == "Europe/Berlin"
