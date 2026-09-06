"""The two timetable tools, driven through a real MCP client.

These cover what the tool layer is responsible for (section 6.1): argument validation, date
defaulting, week resolution, capability refusal and profile tagging. What the payload means
is the mapping layer's business and is tested in `test_mapping.py`.
"""

from __future__ import annotations

import datetime as dt

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError

from tests.fake_untis import TENANT_WITHOUT_MESSAGES, VIEWS_WITHOUT_MESSAGES, FakeUntis
from untis_mcp.config import Config
from untis_mcp.server import create_server
from untis_mcp.untis.client import TIMETABLE_ENTRIES, ClientRegistry, UntisClient

CONFIG = Config.model_validate(
    {
        "profiles": [
            {
                "name": "anna",
                "description": "Anna, Gymnasium Musterstadt",
                "server": "https://ajax.webuntis.com",
                "school": "gym-musterstadt",
                "username": "anna.mustermann",
                "password": "anna-plaintext",
            }
        ]
    }
)


def build(fake: FakeUntis | None = None):
    """A server whose only profile talks to `fake`."""
    fake = fake or FakeUntis().serve_fixture(TIMETABLE_ENTRIES, "timetable_entries.json")
    registry = ClientRegistry(CONFIG)
    registry._clients["anna"] = UntisClient(CONFIG.get("anna"), http=fake.http_client())
    return create_server(CONFIG, registry=registry), fake


# ------------------------------------------------------------------------- the happy path


async def test_the_day_tool_returns_a_timetable_tagged_with_the_profile():
    server, fake = build()

    async with Client(server) as client:
        result = await client.call_tool(
            "get_timetable_day", {"profile": "anna", "date": "2026-08-24"}
        )

    assert result.data.profile == "anna"
    assert result.data.student == "Mustermann"
    assert fake.data_requests == [TIMETABLE_ENTRIES]


async def test_the_day_tool_asks_for_exactly_that_day():
    server, fake = build()

    async with Client(server) as client:
        await client.call_tool("get_timetable_day", {"profile": "anna", "date": "2026-08-24"})

    params = fake.data_params[-1]
    assert params["start"] == "2026-08-24"
    assert params["end"] == "2026-08-24"
    # The v1 view wants the enum name and the *child's* id (sections 5.2, 5.3).
    assert params["resourceType"] == "STUDENT"
    assert params["resources"] == "1001"


async def test_the_day_tool_defaults_to_today():
    """The model frequently omits optional arguments, so the default must be sensible."""
    server, fake = build()

    async with Client(server) as client:
        await client.call_tool("get_timetable_day", {"profile": "anna"})

    assert fake.data_params[-1]["start"] == dt.date.today().isoformat()


async def test_the_week_tool_asks_for_monday_to_sunday():
    server, fake = build()

    async with Client(server) as client:
        # A Thursday.
        await client.call_tool("get_timetable_week", {"profile": "anna", "week_of": "2026-08-27"})

    params = fake.data_params[-1]
    assert params["start"] == "2026-08-24"  # the Monday of that week
    assert params["end"] == "2026-08-30"  # ... through Sunday


async def test_the_week_tool_accepts_an_iso_calendar_week():
    server, fake = build()

    async with Client(server) as client:
        await client.call_tool("get_timetable_week", {"profile": "anna", "year": 2026, "week": 36})

    assert fake.data_params[-1]["start"] == dt.date.fromisocalendar(2026, 36, 1).isoformat()


async def test_the_week_tool_defaults_to_the_current_week():
    server, fake = build()

    async with Client(server) as client:
        await client.call_tool("get_timetable_week", {"profile": "anna"})

    today = dt.date.today()
    assert fake.data_params[-1]["start"] == (today - dt.timedelta(days=today.weekday())).isoformat()


# --------------------------------------------------------------------------- bad arguments


async def test_an_unparseable_date_says_what_a_date_should_look_like():
    server, fake = build()

    async with Client(server) as client:
        with pytest.raises(ToolError) as excinfo:
            await client.call_tool("get_timetable_day", {"profile": "anna", "date": "next tuesday"})

    message = str(excinfo.value)
    assert "next tuesday" in message
    assert "ISO-8601" in message
    assert fake.data_requests == []  # nothing was requested from the school


async def test_half_an_iso_week_is_rejected_with_an_instruction():
    server, _ = build()

    async with Client(server) as client:
        with pytest.raises(ToolError, match="both 'year' and 'week'"):
            await client.call_tool("get_timetable_week", {"profile": "anna", "year": 2026})


async def test_an_impossible_week_number_is_rejected():
    server, _ = build()

    async with Client(server) as client:
        with pytest.raises(ToolError, match="not a valid ISO calendar week"):
            await client.call_tool(
                "get_timetable_week", {"profile": "anna", "year": 2026, "week": 99}
            )


async def test_an_unknown_profile_lists_the_configured_ones():
    server, fake = build()

    async with Client(server) as client:
        with pytest.raises(ToolError) as excinfo:
            await client.call_tool("get_timetable_day", {"profile": "charlie"})

    assert "'anna'" in str(excinfo.value)
    assert fake.login_count == 0  # no school was contacted for a name we do not have


# --------------------------------------------------------------- failures from the school


async def test_a_forbidden_timetable_is_reported_as_a_permanent_answer():
    fake = FakeUntis(forbid_paths={TIMETABLE_ENTRIES})
    server, _ = build(fake)

    async with Client(server) as client:
        with pytest.raises(ToolError) as excinfo:
            await client.call_tool("get_timetable_day", {"profile": "anna"})

    message = str(excinfo.value)
    assert "'anna'" in message
    assert "do not retry" in message


async def test_a_capability_the_account_lacks_is_refused_without_a_request():
    """ADR-010: refuse from the permission list rather than by trying and failing."""
    fake = FakeUntis(
        permissions=["CLASSSERVICE:R:0:ASSIGNED"],  # no TT_VIEW
        views=["TODAY"],  # and no timetable view either
    ).serve_fixture(TIMETABLE_ENTRIES, "timetable_entries.json")
    server, _ = build(fake)

    async with Client(server) as client:
        with pytest.raises(ToolError) as excinfo:
            await client.call_tool("get_timetable_week", {"profile": "anna"})

    message = str(excinfo.value)
    assert "get_timetable_week is not available" in message
    assert "TT_VIEW" in message
    assert fake.data_requests == []


async def test_an_unreachable_school_names_the_profile():
    server, _ = build(FakeUntis(unreachable=True))

    async with Client(server) as client:
        with pytest.raises(ToolError, match="'anna'"):
            await client.call_tool("get_timetable_day", {"profile": "anna"})


# ------------------------------------------------------------------------------ the schema


async def test_both_tools_are_advertised_with_usable_descriptions():
    server, _ = build()

    async with Client(server) as client:
        tools = {tool.name: tool for tool in await client.list_tools()}

    for name in ("get_timetable_day", "get_timetable_week"):
        assert name in tools
        description = tools[name].description or ""
        # The assistant has to learn from the description what a cancellation looks like.
        assert "cancelled" in description or "status" in description
        assert "profile" in tools[name].input_schema["properties"]

    # Only 'profile' is required; every date argument has a default (section 2.2).
    assert tools["get_timetable_day"].input_schema["required"] == ["profile"]


async def test_a_profile_that_lacks_messages_still_gets_its_timetable():
    """The two schools differ, and one difference must not spill onto another tool."""
    fake = FakeUntis(
        permissions=list(TENANT_WITHOUT_MESSAGES), views=list(VIEWS_WITHOUT_MESSAGES)
    ).serve_fixture(TIMETABLE_ENTRIES, "timetable_entries.json")
    server, _ = build(fake)

    async with Client(server) as client:
        result = await client.call_tool("get_timetable_day", {"profile": "anna"})

    assert result.data.profile == "anna"
