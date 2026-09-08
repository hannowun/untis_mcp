"""Homework: the four-list join, and the tool that presents it.

`tests/fixtures/homework.json` is a real `/api/homeworks/lessons` response with the ids and
the teachers' names replaced. What makes this endpoint different from the others is that
nothing is nested: the text, the subject, the teacher and the pupil live in four parallel
lists, and an entry is only complete once all four have been joined by id. Every quirk the
real payload showed is preserved in the fixture — a teacher who is missing from `teachers`,
a lesson whose subject is the empty string, texts that end in a newline.
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError

from tests.fake_untis import STUDENT_ID, TENANT_WITHOUT_MESSAGES, VIEWS_WITHOUT_MESSAGES, FakeUntis
from untis_mcp.config import Config
from untis_mcp.server import create_server
from untis_mcp.untis.client import HOMEWORK, TIMETABLE_ENTRIES, ClientRegistry, UntisClient
from untis_mcp.untis.mapping import parse_homework

FIXTURE = Path(__file__).parent / "fixtures" / "homework.json"
START, END = dt.date(2026, 9, 1), dt.date(2026, 9, 30)

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
    fake = fake or FakeUntis().serve_fixture(HOMEWORK, "homework.json")
    registry = ClientRegistry(CONFIG)
    registry._clients["anna"] = UntisClient(CONFIG.get("anna"), http=fake.http_client())
    return create_server(CONFIG, registry=registry), fake


def homework_payload(
    homeworks: list[dict],
    records: list[dict] | None = None,
    lessons: list[dict] | None = None,
    teachers: list[dict] | None = None,
) -> dict:
    return {
        "data": {
            "records": records or [],
            "homeworks": homeworks,
            "teachers": teachers or [],
            "lessons": lessons or [],
        }
    }


# --------------------------------------------------------------------------------- mapping


@pytest.fixture(scope="module")
def real():
    payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
    return parse_homework(payload, "anna", START, END, student="Mustermann", student_id=STUDENT_ID)


def test_the_four_lists_are_joined_into_one_entry(real):
    """The point of the whole mapping: text, subject and teacher come from three lists."""
    due_first = real.homework[0]

    assert due_first.text == (
        "Hefter- und Bucheinteilung: siehe HA-Heft,\nKopfhörer mit Kabel mitbringen (wenn möglich)"
    )
    assert due_first.subject == "ma"
    assert due_first.teacher == "Mus"
    assert due_first.assigned_date == dt.date(2026, 9, 1)
    assert due_first.due_date == dt.date(2026, 9, 3)
    assert due_first.completed is True


def test_homework_is_ordered_by_when_it_is_due(real):
    assert [item.due_date for item in real.homework] == [
        dt.date(2026, 9, 3),
        dt.date(2026, 9, 4),
        dt.date(2026, 9, 8),
        dt.date(2026, 9, 15),
    ]


def test_the_result_is_tagged_and_the_open_ones_are_counted(real):
    assert real.profile == "anna"
    assert real.student == "Mustermann"
    assert real.start == START and real.end == END
    assert real.open_count == 3  # one of the four is ticked off


def test_a_remark_and_an_attachment_are_reported_next_to_the_text(real):
    zeitstrahl = real.homework[1]

    assert zeitstrahl.text == "Zeitstrahl fertigstellen"
    assert zeitstrahl.remark == "bitte ins Heft kleben"
    assert zeitstrahl.has_attachments is True
    # Its teacher is not in the payload's `teachers` list, which the real one also does.
    assert zeitstrahl.teacher is None


def test_a_subject_the_payload_leaves_blank_stays_null(real):
    """Lesson 29438 exists but carries `subject: ""` — an empty string is not a subject."""
    assert real.homework[3].subject is None


def test_the_short_subject_is_not_invented_into_a_long_one(real):
    """`subject_long` is filled in by the tool from the timetable, never by the mapping."""
    assert all(item.subject_long is None for item in real.homework)


def test_a_teachers_wording_survives_untouched():
    """The messages parser strips HTML; homework must not, or maths loses its inequalities."""
    parsed = parse_homework(
        homework_payload([{"id": 1, "dueDate": 20260908, "text": "Nr. 4: alle x mit 5 < x < 9"}]),
        "anna",
        START,
        END,
    )

    assert parsed.homework[0].text == "Nr. 4: alle x mit 5 < x < 9"


def test_an_entry_with_only_a_remark_still_says_what_to_do():
    parsed = parse_homework(
        homework_payload([{"id": 1, "dueDate": 20260908, "text": "", "remark": "Buch mitbringen"}]),
        "anna",
        START,
        END,
    )

    assert parsed.homework[0].text == "Buch mitbringen"
    assert parsed.homework[0].remark is None


def test_a_missing_due_date_falls_back_to_the_day_it_was_set():
    parsed = parse_homework(
        homework_payload([{"id": 1, "date": 20260907, "text": "Vokabeln"}]), "anna", START, END
    )

    assert parsed.homework[0].due_date == dt.date(2026, 9, 7)
    assert parsed.homework[0].assigned_date == dt.date(2026, 9, 7)


def test_homework_set_for_another_child_is_not_attributed_to_this_one():
    """The endpoint takes no student parameter, so `records` is the only filter there is."""
    parsed = parse_homework(
        homework_payload(
            [
                {"id": 1, "dueDate": 20260908, "text": "Für Anna"},
                {"id": 2, "dueDate": 20260908, "text": "Für das Geschwisterkind"},
                {"id": 3, "dueDate": 20260908, "text": "Ohne Eintrag in records"},
            ],
            records=[
                {"homeworkId": 1, "teacherId": 7, "elementIds": [STUDENT_ID]},
                {"homeworkId": 2, "teacherId": 7, "elementIds": [4711]},
                {"homeworkId": 3, "teacherId": 7, "elementIds": []},
            ],
        ),
        "anna",
        START,
        END,
        student_id=STUDENT_ID,
    )

    assert [item.text for item in parsed.homework] == ["Für Anna", "Ohne Eintrag in records"]


def test_without_a_student_id_nothing_is_filtered_away():
    parsed = parse_homework(
        homework_payload(
            [{"id": 2, "dueDate": 20260908, "text": "Für irgendwen"}],
            records=[{"homeworkId": 2, "teacherId": 7, "elementIds": [4711]}],
        ),
        "anna",
        START,
        END,
    )

    assert len(parsed.homework) == 1


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"data": None},
        {"data": {"homeworks": None}},
        {"data": {"homeworks": ["nonsense"]}},
        {"data": {"homeworks": [{"id": 1, "dueDate": "not-a-date", "text": "x"}]}},
        {"data": {"homeworks": [{"id": 1, "dueDate": 20260908, "text": "   "}]}},
        {"data": {"homeworks": [{}]}},
        {"data": {"homeworks": [{"id": 1, "dueDate": 20260908, "text": "x"}], "records": "junk"}},
    ],
)
def test_a_malformed_payload_yields_an_empty_answer_rather_than_an_exception(payload):
    parsed = parse_homework(payload, "anna", START, END)

    assert parsed.profile == "anna"
    assert parsed.homework == [] or [item.text for item in parsed.homework] == ["x"]


# ------------------------------------------------------------------------------------ tool


async def test_the_tool_asks_the_endpoint_in_its_own_date_format():
    server, fake = build()

    async with Client(server) as client:
        await client.call_tool(
            "get_homework", {"profile": "anna", "start": "2026-09-01", "end": "2026-09-30"}
        )

    params = dict(zip(fake.data_requests, fake.data_params, strict=True))[HOMEWORK]
    assert params["startDate"] == "20260901"
    assert params["endDate"] == "20260930"


async def test_the_default_window_reaches_a_week_back_and_four_weeks_ahead():
    """Overdue homework a parent has not seen yet matters as much as what is still coming."""
    server, fake = build()

    async with Client(server) as client:
        result = await client.call_tool("get_homework", {"profile": "anna"})

    today = dt.date.today()
    # The client reconstructs the result from its schema, where dates are ISO strings.
    assert str(result.data.start) == (today - dt.timedelta(days=7)).isoformat()
    assert str(result.data.end) == (today + dt.timedelta(days=28)).isoformat()


async def test_the_short_subject_is_named_from_the_timetable():
    """'ma' is not what a parent calls the subject; the timetable feed knows 'Mathematik'."""
    fake = FakeUntis().serve_fixture(HOMEWORK, "homework.json")
    fake.serve_fixture(TIMETABLE_ENTRIES, "timetable_entries.json")
    server, _ = build(fake)

    async with Client(server) as client:
        result = await client.call_tool("get_homework", {"profile": "anna"})

    by_subject = {item.subject: item.subject_long for item in result.data.homework}
    assert by_subject["ma"] == "Mathematik"
    assert by_subject["gw"] == "Gesellschaftswissenschaften"


async def test_homework_is_still_returned_when_the_timetable_refuses():
    """The subject names are an improvement, never a dependency (FR-07 in miniature)."""
    fake = FakeUntis(forbid_paths={TIMETABLE_ENTRIES}).serve_fixture(HOMEWORK, "homework.json")
    server, _ = build(fake)

    async with Client(server) as client:
        result = await client.call_tool("get_homework", {"profile": "anna"})

    assert len(result.data.homework) == 4
    assert result.data.homework[0].subject == "ma"
    assert result.data.homework[0].subject_long is None


async def test_only_open_hides_what_has_been_ticked_off():
    server, _ = build()

    async with Client(server) as client:
        result = await client.call_tool(
            "get_homework", {"profile": "anna", "only_open": True, "start": "2026-09-01"}
        )

    assert [item.completed for item in result.data.homework] == [False, False, False]
    # The count is of the whole answer, so it does not change with the filter.
    assert result.data.open_count == 3


async def test_a_reversed_range_is_refused_with_both_dates_named():
    server, _ = build()

    async with Client(server) as client:
        with pytest.raises(ToolError) as excinfo:
            await client.call_tool(
                "get_homework", {"profile": "anna", "start": "2026-09-30", "end": "2026-09-01"}
            )

    assert "2026-09-30" in str(excinfo.value) and "2026-09-01" in str(excinfo.value)


async def test_a_school_that_does_not_grant_homework_is_told_so_permanently():
    """ADR-010: a refusal names the permission and says it is not worth retrying."""
    fake = FakeUntis(
        permissions=[item for item in TENANT_WITHOUT_MESSAGES if not item.startswith("HOMEWORK")],
        views=[item for item in VIEWS_WITHOUT_MESSAGES if item != "STUDENT_HOMEWORK"],
    )
    server, _ = build(fake)

    async with Client(server) as client:
        with pytest.raises(ToolError) as excinfo:
            await client.call_tool("get_homework", {"profile": "anna"})

    message = str(excinfo.value)
    assert "anna" in message
    assert "HOMEWORK" in message
    assert HOMEWORK not in fake.data_requests  # refused before the request was made
