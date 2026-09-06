"""Appointments, from a verified source and an unverified one.

The `EVENT` entries of the timetable feed are real: the fixture contains one the school
actually scheduled. The calendar view is the opposite — it answers on both tenants but has
never returned an entry, so its item shape is guessed. These tests therefore pin two
different things: that real events map correctly, and that the guessed source can neither
corrupt the result nor break the tool.
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import httpx
import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError

from tests.fake_untis import FakeUntis
from untis_mcp.config import Config
from untis_mcp.models import AppointmentSource
from untis_mcp.server import create_server
from untis_mcp.untis.client import CALENDAR_ENTRIES, TIMETABLE_ENTRIES, ClientRegistry, UntisClient
from untis_mcp.untis.mapping import (
    appointments_from_calendar,
    appointments_from_timetable,
    merge_appointments,
    parse_timetable,
)

FIXTURE = Path(__file__).parent / "fixtures" / "timetable_entries.json"
START, END = dt.date(2026, 8, 23), dt.date(2026, 9, 4)

CONFIG = Config.model_validate(
    {
        "profiles": [
            {
                "name": "anna",
                "server": "https://ajax.webuntis.com",
                "school": "gym-musterstadt",
                "username": "anna.mustermann",
                "password": "anna-plaintext",
            }
        ]
    }
)


def build(fake: FakeUntis | None = None):
    fake = fake or FakeUntis().serve_fixture(TIMETABLE_ENTRIES, "timetable_entries.json")
    fake.responses.setdefault(CALENDAR_ENTRIES, {"calendarEntries": []})
    registry = ClientRegistry(CONFIG)
    registry._clients["anna"] = UntisClient(CONFIG.get("anna"), http=fake.http_client())
    return create_server(CONFIG, registry=registry), fake


# ---------------------------------------------------------------- the verified source


@pytest.fixture(scope="module")
def from_timetable():
    payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
    return appointments_from_timetable(parse_timetable(payload, "anna", START, END))


def test_a_real_school_event_becomes_an_appointment(from_timetable):
    assert from_timetable, "the fixture contains a school event"
    event = from_timetable[0]

    assert event.title and event.title != "(unnamed event)"
    assert event.source is AppointmentSource.TIMETABLE
    assert isinstance(event.date, dt.date)


def test_ordinary_lessons_are_not_appointments(from_timetable):
    """A timetable full of lessons must not turn into a list of "appointments"."""
    payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
    all_lessons = sum(len(day.lessons) for day in parse_timetable(payload, "a", START, END).days)

    assert 0 < len(from_timetable) < all_lessons


def test_a_cancelled_event_is_kept_but_marked():
    """A cancelled parents' evening is exactly what a parent must not miss."""
    payload = {
        "days": [
            {
                "date": "2026-09-10",
                "status": "REGULAR",
                "gridEntries": [
                    {
                        "type": "EVENT",
                        "status": "CANCELLED",
                        "duration": {"start": "2026-09-10T18:00", "end": "2026-09-10T20:00"},
                        "lessonInfo": "Elternabend",
                    }
                ],
            }
        ]
    }

    (event,) = appointments_from_timetable(parse_timetable(payload, "anna", START, END))

    assert event.title == "Elternabend"
    assert event.is_cancelled is True
    assert event.start == dt.time(18, 0)


def test_an_unnamed_event_keeps_the_details_it_does_have():
    """One of the two schools enters events with no name at all, only staff and a room.

    Reporting a null title lets the assistant describe the appointment from those details
    instead of presenting an invented name as though the school had given one.
    """
    payload = {
        "days": [
            {
                "date": "2026-09-10",
                "status": "REGULAR",
                "gridEntries": [
                    {
                        "type": "EVENT",
                        "status": "REGULAR",
                        "name": None,
                        "lessonInfo": None,
                        "duration": {"start": "2026-09-10T08:00", "end": "2026-09-10T16:00"},
                        "position1": [
                            {"current": {"type": "TEACHER", "shortName": "Ahl"}, "removed": None}
                        ],
                        "position2": [
                            {"current": {"type": "ROOM", "shortName": "R 313"}, "removed": None}
                        ],
                    }
                ],
            }
        ]
    }

    (event,) = appointments_from_timetable(parse_timetable(payload, "anna", START, END))

    assert event.title is None
    assert event.teachers == ["Ahl"]
    assert event.rooms == ["R 313"]
    assert event.start == dt.time(8, 0)


def test_a_whole_day_event_is_marked_rather_than_starting_at_midnight():
    """Multi-day events arrive as one 00:00-23:59 entry per day."""
    payload = {
        "days": [
            {
                "date": "2026-08-26",
                "status": "REGULAR",
                "gridEntries": [
                    {
                        "type": "EVENT",
                        "status": "REGULAR",
                        "lessonInfo": "Kennenlernwoche",
                        "duration": {"start": "2026-08-26T00:00", "end": "2026-08-26T23:59"},
                    },
                    {
                        "type": "EVENT",
                        "status": "REGULAR",
                        "lessonInfo": "Elternabend",
                        "duration": {"start": "2026-08-26T18:00", "end": "2026-08-26T20:00"},
                    },
                ],
            }
        ]
    }

    whole_day, evening = appointments_from_timetable(parse_timetable(payload, "anna", START, END))

    assert whole_day.all_day is True
    assert evening.all_day is False


# -------------------------------------------------------------- the unverified source


@pytest.mark.parametrize(
    "entry",
    [
        {"startDateTime": "2026-09-10T18:00", "name": "Elternsprechtag"},
        {"start": "2026-09-10T18:00", "title": "Elternsprechtag"},
        {"startTime": "2026-09-10T18:00", "subject": "Elternsprechtag"},
        {"dateTimeFrom": "2026-09-10T18:00", "longName": "Elternsprechtag"},
    ],
)
def test_plausible_calendar_shapes_are_read(entry):
    """The endpoint has never returned data, so several field namings are tolerated."""
    (appointment,) = appointments_from_calendar({"calendarEntries": [entry]})

    assert appointment.date == dt.date(2026, 9, 10)
    assert appointment.title == "Elternsprechtag"
    assert appointment.source is AppointmentSource.CALENDAR


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"calendarEntries": None},
        {"calendarEntries": []},
        {"calendarEntries": ["nonsense"]},
        {"calendarEntries": [{"name": "no date at all"}]},
        {"calendarEntries": [{"startDateTime": "2026-09-10T18:00"}]},  # no title
        {"calendarEntries": [{"startDateTime": "gibberish", "name": "x"}]},
    ],
)
def test_an_unreadable_calendar_entry_is_skipped_not_guessed(payload):
    """A wrong appointment is worse than a missing one."""
    assert appointments_from_calendar(payload) == []


def test_a_calendar_entry_the_timetable_already_reported_is_not_duplicated():
    timetable = appointments_from_timetable(
        parse_timetable(
            {
                "days": [
                    {
                        "date": "2026-09-10",
                        "status": "REGULAR",
                        "gridEntries": [{"type": "EVENT", "lessonInfo": "Projektwoche"}],
                    }
                ]
            },
            "anna",
            START,
            END,
        )
    )
    calendar = appointments_from_calendar(
        {
            "calendarEntries": [
                {"startDateTime": "2026-09-10T08:00", "name": "projektwoche"},  # same, other case
                {"startDateTime": "2026-09-11T18:00", "name": "Elternsprechtag"},
            ]
        }
    )

    merged = merge_appointments("anna", START, END, timetable, calendar)

    assert [item.title for item in merged.appointments] == ["Projektwoche", "Elternsprechtag"]


def test_appointments_are_sorted_by_when_they_happen():
    calendar = appointments_from_calendar(
        {
            "calendarEntries": [
                {"startDateTime": "2026-09-11T18:00", "name": "Later"},
                {"startDateTime": "2026-09-10T08:00", "name": "Earlier"},
            ]
        }
    )

    merged = merge_appointments("anna", START, END, [], calendar)

    assert [item.title for item in merged.appointments] == ["Earlier", "Later"]


# ------------------------------------------------------------------------------ the tool


async def test_the_tool_merges_both_sources():
    fake = FakeUntis().serve_fixture(TIMETABLE_ENTRIES, "timetable_entries.json")
    fake.responses[CALENDAR_ENTRIES] = {
        "calendarEntries": [{"startDateTime": "2026-08-26T18:00", "name": "Elternsprechtag"}]
    }
    server, _ = build(fake)

    async with Client(server) as client:
        result = await client.call_tool(
            "get_appointments", {"profile": "anna", "start": "2026-08-23", "end": "2026-09-04"}
        )

    titles = [item.title for item in result.data.appointments]
    assert "Elternsprechtag" in titles
    assert len(titles) > 1  # ... alongside the school's own event from the timetable
    assert result.data.profile == "anna"


async def test_a_broken_calendar_view_does_not_take_the_tool_down():
    """The unverified source may add appointments; it may not remove the verified ones."""
    fake = FakeUntis(forbid_paths={CALENDAR_ENTRIES}).serve_fixture(
        TIMETABLE_ENTRIES, "timetable_entries.json"
    )
    server, _ = build(fake)

    async with Client(server) as client:
        result = await client.call_tool(
            "get_appointments", {"profile": "anna", "start": "2026-08-23", "end": "2026-09-04"}
        )

    assert result.data.appointments, "the timetable events must survive a failing calendar"
    assert all(item.source == AppointmentSource.TIMETABLE for item in result.data.appointments)


async def test_a_failing_timetable_does_take_the_tool_down():
    """The verified source is the tool; losing it silently would be worse than an error."""
    fake = FakeUntis(forbid_paths={TIMETABLE_ENTRIES})
    server, _ = build(fake)

    async with Client(server) as client:
        with pytest.raises(ToolError, match="'anna'"):
            await client.call_tool("get_appointments", {"profile": "anna"})


async def test_the_default_window_looks_four_weeks_ahead():
    server, fake = build()

    async with Client(server) as client:
        await client.call_tool("get_appointments", {"profile": "anna"})

    params = next(
        p
        for p, path in zip(fake.data_params, fake.data_requests, strict=True)
        if path == TIMETABLE_ENTRIES
    )
    today = dt.date.today()
    assert params["start"] == today.isoformat()
    assert params["end"] == (today + dt.timedelta(days=28)).isoformat()


async def test_the_calendar_call_uses_the_discovered_parameter_set():
    server, fake = build()

    async with Client(server) as client:
        await client.call_tool(
            "get_appointments", {"profile": "anna", "start": "2026-09-01", "end": "2026-09-30"}
        )

    params = next(
        p
        for p, path in zip(fake.data_params, fake.data_requests, strict=True)
        if path == CALENDAR_ENTRIES
    )
    # elementType is numeric here, and the range wants timestamps (section 5.3).
    assert params["elementType"] == "5"
    assert params["elementId"] == "1001"
    assert params["startDateTime"] == "2026-09-01T00:00"
    assert params["endDateTime"] == "2026-09-30T23:59"


async def test_a_reversed_range_is_rejected_before_any_request():
    server, fake = build()

    async with Client(server) as client:
        with pytest.raises(ToolError, match="lies before"):
            await client.call_tool(
                "get_appointments", {"profile": "anna", "start": "2026-09-30", "end": "2026-09-01"}
            )

    assert fake.data_requests == []


async def test_the_tool_is_advertised_with_only_profile_required():
    server, _ = build()

    async with Client(server) as client:
        tools = {tool.name: tool for tool in await client.list_tools()}

    schema = tools["get_appointments"].input_schema
    assert schema["required"] == ["profile"]
    assert "exams" in (tools["get_appointments"].description or "").lower()


async def test_a_calendar_view_that_returns_nonsense_is_ignored():
    fake = FakeUntis().serve_fixture(TIMETABLE_ENTRIES, "timetable_entries.json")
    fake.responses[CALENDAR_ENTRIES] = {"calendarEntries": [{"unexpected": "shape"}]}
    server, _ = build(fake)

    async with Client(server) as client:
        result = await client.call_tool(
            "get_appointments", {"profile": "anna", "start": "2026-08-23", "end": "2026-09-04"}
        )

    assert all(item.source == AppointmentSource.TIMETABLE for item in result.data.appointments)


async def test_a_non_json_calendar_response_is_survivable():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == CALENDAR_ENTRIES:
            return httpx.Response(200, text="<html>maintenance</html>")
        return fake.handler(request)

    fake = FakeUntis().serve_fixture(TIMETABLE_ENTRIES, "timetable_entries.json")
    registry = ClientRegistry(CONFIG)
    registry._clients["anna"] = UntisClient(
        CONFIG.get("anna"), http=httpx.AsyncClient(transport=httpx.MockTransport(handler))
    )

    async with Client(create_server(CONFIG, registry=registry)) as client:
        result = await client.call_tool(
            "get_appointments", {"profile": "anna", "start": "2026-08-23", "end": "2026-09-04"}
        )

    assert result.data.appointments


def test_the_slices_of_a_multi_day_event_read_as_a_span():
    """The real shape: 08:00-23:59, then 00:00-23:59, then 00:00-16:35 on consecutive days."""

    def day(date: str, start: str, end: str) -> dict:
        return {
            "date": date,
            "status": "REGULAR",
            "gridEntries": [
                {
                    "type": "EVENT",
                    "status": "REGULAR",
                    "lessonInfo": "Kennenlernwoche",
                    "duration": {"start": f"{date}T{start}", "end": f"{date}T{end}"},
                }
            ],
        }

    payload = {
        "days": [
            day("2026-08-25", "08:00", "23:59"),
            day("2026-08-26", "00:00", "23:59"),
            day("2026-08-28", "00:00", "16:35"),
        ]
    }

    first, middle, last = appointments_from_timetable(parse_timetable(payload, "anna", START, END))

    # Day one starts for real and runs to the end of the day.
    assert (first.start, first.end, first.all_day) == (dt.time(8, 0), None, False)
    # The middle day is nothing but boundaries.
    assert (middle.start, middle.end, middle.all_day) == (None, None, True)
    # The last day ends for real; midnight was never an appointment.
    assert (last.start, last.end, last.all_day) == (None, dt.time(16, 35), False)
