"""Mapping tests against a payload captured from a real tenant.

`tests/fixtures/timetable_entries.json` is five days lifted out of a live
`view/v1/timetable/entries` response and anonymised: an empty day, a cancellation, a
substitution, a school event and an ordinary day. This is the layer the design document
calls the most fragile one (section 9), so the tests assert on the awkward cases rather
than the happy path.
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import pytest

from untis_mcp.models import EntryKind, LessonStatus
from untis_mcp.untis.mapping import parse_timetable

FIXTURE = Path(__file__).parent / "fixtures" / "timetable_entries.json"
START, END = dt.date(2026, 8, 23), dt.date(2026, 9, 4)


@pytest.fixture(scope="module")
def payload() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def timetable(payload):
    return parse_timetable(payload, profile="anna", start=START, end=END, student="Mustermann")


def day_of(timetable, iso: str):
    return next(day for day in timetable.days if day.date == dt.date.fromisoformat(iso))


# --------------------------------------------------------------------------------- shape


def test_the_result_is_tagged_with_the_profile_and_the_child(timetable):
    assert timetable.profile == "anna"
    assert timetable.student == "Mustermann"
    assert timetable.start == START and timetable.end == END


def test_days_are_sorted_and_lessons_are_ordered_by_start_time(timetable):
    assert [day.date for day in timetable.days] == sorted(day.date for day in timetable.days)
    for day in timetable.days:
        starts = [lesson.start for lesson in day.lessons if lesson.start]
        assert starts == sorted(starts)


def test_a_day_outside_the_published_plan_is_marked_rather_than_dropped(timetable):
    """An empty Sunday must not look like a failed request."""
    empty = day_of(timetable, "2026-08-23")

    assert empty.has_data is False
    assert empty.lessons == []


def test_times_come_from_iso_timestamps_not_from_a_time_grid(timetable):
    """ADR-009: the v1 feed carries full timestamps, so no Hmm decoding is involved."""
    lesson = day_of(timetable, "2026-09-04").lessons[0]

    assert isinstance(lesson.start, dt.time)
    assert lesson.start.minute in {0, 5, 15, 30, 45, 50}
    assert lesson.end > lesson.start


# ------------------------------------------------------------------ the interesting cases


def test_a_cancelled_lesson_keeps_its_subject_teacher_and_room(timetable):
    """A parent needs to know *what* was dropped, not merely that something was."""
    cancelled = [
        lesson
        for lesson in day_of(timetable, "2026-08-24").lessons
        if lesson.status is LessonStatus.CANCELLED
    ]

    assert cancelled, "the fixture day is supposed to contain a cancellation"
    lesson = cancelled[0]
    assert lesson.subject
    assert lesson.teachers
    assert lesson.status_raw == "CANCELLED"


def test_a_substitution_reports_both_who_teaches_and_who_was_replaced(timetable):
    replaced = [
        lesson
        for day in timetable.days
        for lesson in day.lessons
        if lesson.replaced_teachers or lesson.replaced_rooms
    ]

    assert replaced, "the fixture is supposed to contain a substitution"
    lesson = replaced[0]
    # The two lists are different people/rooms: current cover vs. the original assignment.
    assert set(lesson.teachers) != set(lesson.replaced_teachers) or not lesson.teachers


def test_a_withdrawn_teacher_without_cover_survives_mapping(timetable):
    """Some slots carry `removed` with `current: null` — dereferencing blindly loses them."""
    withdrawn = [
        lesson
        for day in timetable.days
        for lesson in day.lessons
        if lesson.replaced_teachers and not lesson.teachers
    ]

    assert withdrawn, "the fixture contains a slot whose current half is null"


def test_a_school_event_is_recognised_and_named(timetable):
    events = [
        lesson for day in timetable.days for lesson in day.lessons if lesson.kind is EntryKind.EVENT
    ]

    assert events, "the fixture is supposed to contain a school event"
    # An event names itself in an INFO position rather than in a subject.
    assert any(event.info for event in events)


def test_no_lesson_repeats_its_info_as_a_note(timetable):
    """Every note in this fixture duplicated the lesson info, and duplicates are dropped.

    The case where a note genuinely adds something is covered by
    `test_a_note_that_adds_something_is_kept_alongside_the_info`.
    """
    for day in timetable.days:
        for lesson in day.lessons:
            if lesson.note is not None:
                assert lesson.note.strip()
                assert lesson.note.strip() != (lesson.info or "").strip()


# ------------------------------------------------------------------- defensive behaviour


def test_an_unknown_status_becomes_unknown_instead_of_failing():
    """The endpoints are undocumented; a new status must not break a whole timetable."""
    payload = {
        "days": [
            {
                "date": "2026-09-07",
                "status": "REGULAR",
                "gridEntries": [
                    {
                        "duration": {"start": "2026-09-07T08:00", "end": "2026-09-07T08:45"},
                        "type": "SOMETHING_NEW",
                        "status": "RESCHEDULED_SOMEHOW",
                        "position2": [
                            {"current": {"type": "SUBJECT", "shortName": "de"}, "removed": None}
                        ],
                    }
                ],
            }
        ]
    }

    lesson = parse_timetable(payload, "anna", START, END).days[0].lessons[0]

    assert lesson.status is LessonStatus.UNKNOWN
    assert lesson.status_raw == "RESCHEDULED_SOMEHOW"  # the real value stays visible
    assert lesson.kind is EntryKind.OTHER
    assert lesson.subject == "de"


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"days": None},
        {"days": []},
        {"days": [{"date": "not-a-date"}]},
        {"days": [{"date": "2026-09-07", "gridEntries": None}]},
        {"days": [{"date": "2026-09-07", "gridEntries": ["nonsense"]}]},
        {"days": ["nonsense"]},
        {"days": [{"date": "2026-09-07", "gridEntries": [{"duration": "broken"}]}]},
    ],
)
def test_a_malformed_payload_yields_a_partial_result_rather_than_an_exception(payload):
    """Half a timetable beats an error message (section 9)."""
    timetable = parse_timetable(payload, "anna", START, END)

    assert timetable.profile == "anna"
    assert isinstance(timetable.days, list)


def test_a_lesson_without_a_usable_time_is_kept_with_null_times():
    payload = {
        "days": [
            {
                "date": "2026-09-07",
                "status": "REGULAR",
                "gridEntries": [{"status": "REGULAR", "type": "NORMAL_TEACHING_PERIOD"}],
            }
        ]
    }

    lesson = parse_timetable(payload, "anna", START, END).days[0].lessons[0]

    assert lesson.start is None and lesson.end is None
    assert lesson.status is LessonStatus.REGULAR


def test_the_compact_date_format_of_the_older_endpoints_is_accepted():
    """Other endpoints answer in yyyyMMdd; the same mapper should not trip over it."""
    payload = {"days": [{"date": 20260907, "status": "REGULAR", "gridEntries": []}]}

    assert parse_timetable(payload, "anna", START, END).days[0].date == dt.date(2026, 9, 7)


def test_element_type_decides_the_role_not_the_position_index():
    """The two tenants do not agree on position ordering, so the type field is the source."""
    payload = {
        "days": [
            {
                "date": "2026-09-07",
                "status": "REGULAR",
                "gridEntries": [
                    {
                        "status": "REGULAR",
                        "type": "NORMAL_TEACHING_PERIOD",
                        # Deliberately the wrong way round compared with the observed feed.
                        "position1": [
                            {"current": {"type": "ROOM", "shortName": "R 101"}, "removed": None}
                        ],
                        "position3": [
                            {"current": {"type": "TEACHER", "shortName": "Ahl"}, "removed": None}
                        ],
                    }
                ],
            }
        ]
    }

    lesson = parse_timetable(payload, "anna", START, END).days[0].lessons[0]

    assert lesson.rooms == ["R 101"]
    assert lesson.teachers == ["Ahl"]


def test_a_note_that_merely_repeats_the_info_is_dropped():
    """Schools often echo the lesson info into the free-text fields; once is enough."""
    payload = {
        "days": [
            {
                "date": "2026-09-07",
                "status": "REGULAR",
                "gridEntries": [
                    {
                        "status": "REGULAR",
                        "type": "NORMAL_TEACHING_PERIOD",
                        "lessonInfo": "Geo/NaWi",
                        "lessonText": "Geo/NaWi",
                        "position2": [
                            {"current": {"type": "INFO", "shortName": "Geo/NaWi"}, "removed": None}
                        ],
                    }
                ],
            }
        ]
    }

    lesson = parse_timetable(payload, "anna", START, END).days[0].lessons[0]

    assert lesson.info == "Geo/NaWi"
    assert lesson.note is None


def test_a_note_that_adds_something_is_kept_alongside_the_info():
    payload = {
        "days": [
            {
                "date": "2026-09-07",
                "status": "REGULAR",
                "gridEntries": [
                    {
                        "status": "CANCELLED",
                        "type": "NORMAL_TEACHING_PERIOD",
                        "lessonInfo": "NaWi",
                        "substitutionText": "faellt aus, Herr Ahl krank",
                    }
                ],
            }
        ]
    }

    lesson = parse_timetable(payload, "anna", START, END).days[0].lessons[0]

    assert lesson.info == "NaWi"
    assert lesson.note == "faellt aus, Herr Ahl krank"
