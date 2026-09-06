"""Absences: the wire-format conversions and the tool around them.

`tests/fixtures/absences.json` is a real `/api/classreg/absences/students` response with the
child's and the staff's names replaced. It is the endpoint that speaks the *old* formats of
section 5.3 — `yyyyMMdd` as a number, unpadded `Hmm` times — so most of what can go wrong
here is a conversion, not a shape.
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError

from tests.fake_untis import FakeUntis
from untis_mcp.config import Config
from untis_mcp.server import create_server
from untis_mcp.untis.client import ABSENCES, ClientRegistry, UntisClient
from untis_mcp.untis.mapping import parse_absences

FIXTURE = Path(__file__).parent / "fixtures" / "absences.json"
START, END = dt.date(2026, 8, 1), dt.date(2026, 9, 30)

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
    fake = fake or FakeUntis().serve_fixture(ABSENCES, "absences.json")
    registry = ClientRegistry(CONFIG)
    registry._clients["anna"] = UntisClient(CONFIG.get("anna"), http=fake.http_client())
    return create_server(CONFIG, registry=registry), fake


def absence_payload(*entries: dict) -> dict:
    return {"data": {"absences": list(entries), "absenceReasons": [], "excuseStatuses": None}}


# --------------------------------------------------------------------------------- mapping


@pytest.fixture(scope="module")
def real():
    payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
    return parse_absences(payload, "anna", START, END, student="Mustermann")


def test_the_old_wire_formats_are_decoded(real):
    """20260903 is a date and 800 is 08:00 — neither is a number to be shown to a parent."""
    absence = real.absences[0]

    assert absence.start_date == dt.date(2026, 9, 3)
    assert absence.end_date == dt.date(2026, 9, 3)
    assert absence.start_time == dt.time(8, 0)
    assert absence.end_time == dt.time(16, 0)


def test_reason_excuse_and_note_survive(real):
    absence = real.absences[0]

    assert absence.is_excused is True
    assert absence.excuse_status == "entschuldigt"
    assert absence.reason == "Entschuldigungsschreiben der Eltern"
    assert absence.note == "E-Mail"
    assert absence.recorded_by == "Sekr"


def test_the_result_is_tagged_and_counted(real):
    assert real.profile == "anna"
    assert real.student == "Mustermann"
    assert real.unexcused_count == 0


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (800, dt.time(8, 0)),
        (830, dt.time(8, 30)),
        (1600, dt.time(16, 0)),
        ("0805", dt.time(8, 5)),
        (5, dt.time(0, 5)),
        (0, dt.time(0, 0)),
        (None, None),
        ("", None),
        ("not a time", None),
        (2500, None),  # implausible rather than crashing
        (899, None),  # 99 minutes
    ],
)
def test_unpadded_clock_times_of_every_shape(value, expected):
    parsed = parse_absences(
        absence_payload({"startDate": 20260903, "startTime": value}), "anna", START, END
    )
    assert parsed.absences[0].start_time == expected


def test_an_unexcused_absence_is_recognised_and_counted():
    parsed = parse_absences(
        absence_payload(
            {"startDate": 20260901, "isExcused": False, "excuseStatus": "unentschuldigt"},
            {"startDate": 20260902, "isExcused": True, "excuseStatus": "entschuldigt"},
        ),
        "anna",
        START,
        END,
    )

    assert parsed.unexcused_count == 1
    assert parsed.absences[0].is_excused is False


def test_a_missing_flag_falls_back_to_the_wording_and_errs_towards_unexcused():
    """Reporting an unexcused absence as excused would hide the thing a parent must act on."""
    parsed = parse_absences(
        absence_payload(
            {"startDate": 20260901, "excuseStatus": "unentschuldigt"},
            {"startDate": 20260902, "excuseStatus": "entschuldigt"},
            {"startDate": 20260903, "excuseStatus": "ungeklärt"},
            {"startDate": 20260904},
        ),
        "anna",
        START,
        END,
    )

    assert [item.is_excused for item in parsed.absences] == [False, True, False, False]
    assert parsed.unexcused_count == 3


def test_the_nested_excuse_record_is_used_when_the_top_level_is_silent():
    parsed = parse_absences(
        absence_payload(
            {
                "startDate": 20260901,
                "excuse": {"isExcused": True, "excuseStatus": "entschuldigt", "text": "Attest"},
            }
        ),
        "anna",
        START,
        END,
    )

    absence = parsed.absences[0]
    assert absence.is_excused is True
    assert absence.excuse_status == "entschuldigt"
    assert absence.excuse_note == "Attest"


def test_absences_are_sorted_by_when_they_started():
    parsed = parse_absences(
        absence_payload(
            {"startDate": 20260910, "startTime": 800},
            {"startDate": 20260901, "startTime": 1200},
            {"startDate": 20260901, "startTime": 800},
        ),
        "anna",
        START,
        END,
    )

    assert [(a.start_date, a.start_time) for a in parsed.absences] == [
        (dt.date(2026, 9, 1), dt.time(8, 0)),
        (dt.date(2026, 9, 1), dt.time(12, 0)),
        (dt.date(2026, 9, 10), dt.time(8, 0)),
    ]


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"data": None},
        {"data": {"absences": None}},
        {"data": {"absences": ["nonsense"]}},
        {"data": {"absences": [{"startDate": "not-a-date"}]}},
        {"data": {"absences": [{}]}},
    ],
)
def test_a_malformed_payload_yields_an_empty_answer_rather_than_an_exception(payload):
    parsed = parse_absences(payload, "anna", START, END)

    assert parsed.profile == "anna"
    assert parsed.absences == []


def test_an_absence_without_an_end_date_spans_its_start_day():
    parsed = parse_absences(absence_payload({"startDate": 20260903}), "anna", START, END)

    assert parsed.absences[0].end_date == dt.date(2026, 9, 3)


# ------------------------------------------------------------------------------- the tool


async def test_the_tool_returns_the_absences_of_the_configured_child():
    server, fake = build()

    async with Client(server) as client:
        result = await client.call_tool(
            "get_absences", {"profile": "anna", "start": "2026-09-01", "end": "2026-09-30"}
        )

    assert result.data.profile == "anna"
    assert len(result.data.absences) == 1
    params = fake.data_params[-1]
    # The compact format and the *child's* id, not the parent's (sections 5.1, 5.3).
    assert params["startDate"] == "20260901"
    assert params["endDate"] == "20260930"
    assert params["studentId"] == "1001"
    assert params["excuseStatusId"] == "-1"


async def test_the_default_range_is_the_school_year_so_far():
    """A parent asking about unexcused absences means the school year, not the last week."""
    server, fake = build()

    async with Client(server) as client:
        await client.call_tool("get_absences", {"profile": "anna"})

    # The fake reports a school year starting 2026-08-03.
    assert fake.data_params[-1]["startDate"] == "20260803"
    assert fake.data_params[-1]["endDate"] == dt.date.today().strftime("%Y%m%d")


async def test_only_unexcused_filters_the_answer():
    fake = FakeUntis()
    fake.responses[ABSENCES] = absence_payload(
        {"startDate": 20260901, "isExcused": False, "excuseStatus": "unentschuldigt"},
        {"startDate": 20260902, "isExcused": True, "excuseStatus": "entschuldigt"},
    )
    server, _ = build(fake)

    async with Client(server) as client:
        result = await client.call_tool("get_absences", {"profile": "anna", "only_unexcused": True})

    assert len(result.data.absences) == 1
    assert result.data.absences[0].is_excused is False
    # The count still describes what was found, so "1 of 2" stays visible.
    assert result.data.unexcused_count == 1


async def test_a_reversed_range_is_rejected_before_any_request():
    server, fake = build()

    async with Client(server) as client:
        with pytest.raises(ToolError, match="lies before"):
            await client.call_tool(
                "get_absences", {"profile": "anna", "start": "2026-09-30", "end": "2026-09-01"}
            )

    assert ABSENCES not in fake.data_requests


async def test_an_account_without_the_absence_permission_is_refused():
    fake = FakeUntis(permissions=["TT_VIEW:R:5:ASSIGNED"], views=["TIMETABLE_NEW_STUDENTS_MY"])
    server, _ = build(fake)

    async with Client(server) as client:
        with pytest.raises(ToolError) as excinfo:
            await client.call_tool("get_absences", {"profile": "anna"})

    message = str(excinfo.value)
    assert "get_absences is not available" in message
    assert "STUDENTABSENCES" in message


async def test_the_tool_is_advertised_with_only_profile_required():
    server, _ = build()

    async with Client(server) as client:
        tools = {tool.name: tool for tool in await client.list_tools()}

    schema = tools["get_absences"].input_schema
    assert schema["required"] == ["profile"]
    assert set(schema["properties"]) == {"profile", "start", "end", "only_unexcused"}
