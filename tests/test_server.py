"""End-to-end tests for the server: a real MCP client talking to a real server instance.

FastMCP's in-memory transport connects a `Client` straight to a `FastMCP` object, so these
exercise tool registration, schema generation and serialisation without a subprocess or a
socket — which is what "proves the profile mechanism end to end" (step 1 of section 11) is
supposed to mean.
"""

from __future__ import annotations

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError

from untis_mcp.config import Config
from untis_mcp.server import create_server
from untis_mcp.tools import resolve_profile


def make_config(**overrides) -> Config:
    profiles = [
        {
            "name": "anna",
            "description": "Anna, Gymnasium Musterstadt",
            "server": "https://ajax.webuntis.com",
            "school": "gym-musterstadt",
            "username": "anna.mustermann",
            "password": "anna-plaintext",
        },
        {
            "name": "Ben",
            "description": "Ben, Realschule Beispieldorf",
            "server": "https://herakles.webuntis.com",
            "school": "rs-beispieldorf",
            "username": "ben.mustermann",
            "password": "ben-plaintext",
        },
    ]
    return Config.model_validate({"profiles": overrides.get("profiles", profiles)})


@pytest.fixture
def server():
    return create_server(make_config())


# --------------------------------------------------------------------------- the protocol


async def test_the_server_advertises_list_profiles(server):
    async with Client(server) as client:
        tools = await client.list_tools()

    by_name = {tool.name: tool for tool in tools}
    assert "list_profiles" in by_name
    # The description is what tells the assistant to call this first.
    assert by_name["list_profiles"].description
    assert "profile" in by_name["list_profiles"].description.lower()


async def test_list_profiles_returns_the_configured_profiles(server):
    async with Client(server) as client:
        result = await client.call_tool("list_profiles")

    profiles = result.data
    assert [item.name for item in profiles] == ["anna", "Ben"]
    assert profiles[0].description == "Anna, Gymnasium Musterstadt"
    # Capabilities need a login, which step 1 does not do; null says "unknown", not "none".
    assert profiles[0].supported_tools is None


async def test_list_profiles_never_returns_a_secret(server):
    async with Client(server) as client:
        result = await client.call_tool("list_profiles")

    payload = str(result.structured_content) + str(result.content)
    for forbidden in ("anna-plaintext", "ben-plaintext", "anna.mustermann", "password"):
        assert forbidden not in payload


async def test_a_single_profile_configuration_works(server):
    single = create_server(
        make_config(
            profiles=[
                {
                    "name": "solo",
                    "server": "https://ajax.webuntis.com",
                    "school": "gym",
                    "username": "u",
                    "password": "p",
                }
            ]
        )
    )
    async with Client(single) as client:
        result = await client.call_tool("list_profiles")

    assert [item.name for item in result.data] == ["solo"]
    assert result.data[0].description == ""


# ------------------------------------------------------------------- profile resolution


def test_resolve_profile_translates_an_unknown_name_into_a_tool_error():
    """FR-04 through the tool layer: the assistant has to see the valid names."""
    config = make_config()

    with pytest.raises(ToolError) as excinfo:
        resolve_profile(config, "charlie")

    message = str(excinfo.value)
    assert "'charlie'" in message
    assert "'anna'" in message and "'Ben'" in message


def test_resolve_profile_returns_the_profile_for_a_known_name():
    config = make_config()

    assert resolve_profile(config, "BEN").username == "ben.mustermann"


# -------------------------------------------------------------------------------- auth


def test_a_configured_token_turns_on_authentication():
    """ADR-006: the optional bearer guard for the HTTP transport."""
    assert create_server(make_config(), auth_token="s3kret").auth is not None
    assert create_server(make_config(), auth_token=None).auth is None
    assert create_server(make_config(), auth_token="").auth is None


# ------------------------------------------------------------------ reported capabilities


async def test_list_profiles_reports_capabilities_once_a_profile_has_been_used():
    """FR-09: after a login the assistant learns which tools are worth calling."""
    from tests.fake_untis import TENANT_WITHOUT_MESSAGES, VIEWS_WITHOUT_MESSAGES, FakeUntis
    from untis_mcp.untis.client import ClientRegistry, UntisClient

    config = make_config()
    registry = ClientRegistry(config)
    # "Ben" attends the school that forbids the message inbox; "anna" has not been used yet.
    ben = UntisClient(
        config.get("Ben"),
        http=FakeUntis(
            permissions=list(TENANT_WITHOUT_MESSAGES), views=list(VIEWS_WITHOUT_MESSAGES)
        ).http_client(),
    )
    registry._clients["Ben"] = ben
    await ben.session()

    async with Client(create_server(config, registry=registry)) as client:
        result = await client.call_tool("list_profiles")

    by_name = {item.name: item for item in result.data}
    assert by_name["anna"].supported_tools is None  # never contacted, so unknown
    assert "get_messages" not in by_name["Ben"].supported_tools
    assert "get_timetable_week" in by_name["Ben"].supported_tools
    assert "get_messages_of_day" in by_name["Ben"].supported_tools


async def test_list_profiles_still_answers_when_a_school_is_unreachable():
    """FR-07: profile discovery must not depend on any school being up."""
    from tests.fake_untis import FakeUntis
    from untis_mcp.untis.client import ClientRegistry, UntisClient

    config = make_config()
    registry = ClientRegistry(config)
    registry._clients["Ben"] = UntisClient(
        config.get("Ben"), http=FakeUntis(unreachable=True).http_client()
    )

    async with Client(create_server(config, registry=registry)) as client:
        result = await client.call_tool("list_profiles")

    assert [item.name for item in result.data] == ["anna", "Ben"]
