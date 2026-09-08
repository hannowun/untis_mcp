"""The tool results have to satisfy the schemas the tools advertise.

An MCP client validates every structured result against the tool's `output_schema` and
refuses the whole answer when it does not fit, so a mismatch here is not cosmetic: it makes
a tool unusable. It happened once already — `datetime.time` is described as
`format: "time"`, which clients read as RFC 3339 *full-time*, where a zone offset is
mandatory, while we emit school-local wall-clock times without one. These tests keep both
halves honest: the schema must not promise a zoned format, and the values we emit must pass
a validator as strict as the clients' own.
"""

from __future__ import annotations

import datetime as dt
import re
from collections.abc import Iterator
from typing import Any

import pytest
from fastmcp import Client
from jsonschema import Draft202012Validator, FormatChecker

from tests.test_server import make_config
from untis_mcp import models
from untis_mcp.server import create_server

# Formats whose RFC 3339 production requires a zone offset. We have no zone to offer — the
# schools report wall-clock times — so no output schema may claim one.
ZONED_FORMATS = frozenset({"time", "date-time"})

RFC3339_TIME = re.compile(r"^\d\d:\d\d:\d\d(\.\d+)?([Zz]|[+-]\d\d:?\d\d)$")
RFC3339_DATE_TIME = re.compile(r"^\d{4}-\d\d-\d\d[Tt ].+$")

strict_formats = FormatChecker()


@strict_formats.checks("time")
def _rfc3339_time(value: object) -> bool:
    """As strict as a client's validator: a bare '08:00:00' is not a valid full-time."""
    return not isinstance(value, str) or bool(RFC3339_TIME.match(value))


@strict_formats.checks("date-time")
def _rfc3339_date_time(value: object) -> bool:
    if not isinstance(value, str):
        return True
    date, _, time = value.partition("T")
    return bool(RFC3339_DATE_TIME.match(value)) and bool(RFC3339_TIME.match(time))


def formats_in(schema: Any, path: str = "") -> Iterator[tuple[str, str]]:
    """Every `format` in a schema, with the path it sits at."""
    if isinstance(schema, dict):
        if isinstance(schema.get("format"), str):
            yield path, schema["format"]
        for key, value in schema.items():
            child = (
                path if key in {"anyOf", "allOf", "oneOf", "items", "$defs"} else f"{path}/{key}"
            )
            yield from formats_in(value, child)
    elif isinstance(schema, list):
        for item in schema:
            yield from formats_in(item, path)


def sample_results() -> dict[str, Any]:
    """One instance of every result model, with the optional times deliberately filled."""
    lesson = models.Lesson(
        date=dt.date(2026, 9, 9), start=dt.time(8, 0), end=dt.time(8, 45), subject="de"
    )
    return {
        "Timetable": models.Timetable(
            profile="anna",
            start=lesson.date,
            end=lesson.date,
            days=[models.TimetableDay(date=lesson.date, lessons=[lesson])],
        ),
        "Absences": models.Absences(
            profile="anna",
            start=lesson.date,
            end=lesson.date,
            absences=[
                models.Absence(
                    start_date=lesson.date,
                    end_date=lesson.date,
                    start_time=dt.time(8, 0),
                    end_time=dt.time(9, 30),
                    is_excused=False,
                )
            ],
        ),
        "Appointments": models.Appointments(
            profile="anna",
            start=lesson.date,
            end=lesson.date,
            appointments=[
                models.Appointment(
                    date=lesson.date, start=dt.time(14, 0), end=dt.time(16, 35), title="Wandertag"
                )
            ],
        ),
        "Messages": models.Messages(
            profile="anna",
            messages=[models.Message(subject="Elternabend", sent=dt.datetime(2026, 9, 7, 8, 5))],
        ),
        "MessagesOfDay": models.MessagesOfDay(
            profile="anna",
            date=lesson.date,
            messages=[models.MessageOfDay(text="Die Turnhalle bleibt gesperrt.")],
        ),
    }


@pytest.mark.parametrize("name", sorted(sample_results()))
def test_a_result_satisfies_the_schema_it_is_described_by(name):
    result = sample_results()[name]
    schema = type(result).model_json_schema(mode="serialization")

    errors = list(
        Draft202012Validator(schema, format_checker=strict_formats).iter_errors(
            result.model_dump(mode="json")
        )
    )

    assert errors == [], [f"{'/'.join(str(part) for part in e.path)}: {e.message}" for e in errors]


async def test_no_tool_promises_a_format_that_needs_a_time_zone():
    """The regression itself: `format: "time"` made three tools unusable."""
    async with Client(create_server(make_config())) as client:
        tools = await client.list_tools()

    zoned = [
        f"{tool.name}{path}: format={found}"
        for tool in tools
        for path, found in formats_in(tool.output_schema)
        if found in ZONED_FORMATS
    ]

    assert zoned == []


def test_times_are_emitted_as_local_wall_clock():
    """What the assistant actually reads: '08:00', not '08:00:00' and not a fake offset."""
    lesson = models.Lesson(date=dt.date(2026, 9, 9), start=dt.time(8, 0), end=dt.time(8, 45))
    message = models.Message(sent=dt.datetime(2026, 9, 7, 8, 5))

    assert lesson.model_dump(mode="json")["start"] == "08:00"
    assert lesson.model_dump(mode="json")["end"] == "08:45"
    assert message.model_dump(mode="json")["sent"] == "2026-09-07T08:05"
    # Python-mode dumps keep the real objects, which the rest of the code sorts by.
    assert lesson.model_dump()["start"] == dt.time(8, 0)
