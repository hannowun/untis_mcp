"""Domain models returned by the tools (ADR-004).

FastMCP derives each tool's output schema from these, so field descriptions are part of the
contract the assistant sees — they are written for a model to read.
"""

from __future__ import annotations

import datetime as dt
from enum import StrEnum

from pydantic import BaseModel, Field


class ProfileSummary(BaseModel):
    """One configured child/school, as reported by `list_profiles`. Never holds secrets."""

    name: str = Field(description="Pass this as the 'profile' argument of every other tool.")
    description: str = Field(
        default="",
        description="Who this profile is for, e.g. which child and which school.",
    )
    supported_tools: list[str] | None = Field(
        default=None,
        description=(
            "Tools this profile's account is permitted to use. Null means it has not been "
            "determined yet, because that requires logging in to the school's server; it "
            "does not mean the tools are unavailable."
        ),
    )


class LessonStatus(StrEnum):
    """What WebUntis says happened to a lesson.

    Unknown values map to `unknown` rather than failing: these endpoints are undocumented
    and a new status must not take a whole timetable down (section 5).
    """

    REGULAR = "regular"
    CANCELLED = "cancelled"
    CHANGED = "changed"
    ADDITIONAL = "additional"
    UNKNOWN = "unknown"

    @classmethod
    def _missing_(cls, value: object) -> LessonStatus:
        return cls.UNKNOWN


class EntryKind(StrEnum):
    """What kind of thing occupies the slot."""

    LESSON = "lesson"
    EVENT = "event"
    ADDITIONAL = "additional"
    OTHER = "other"

    @classmethod
    def _missing_(cls, value: object) -> EntryKind:
        return cls.OTHER


class Lesson(BaseModel):
    """One entry in the timetable: a lesson, an exam slot or a school event."""

    date: dt.date
    start: dt.time | None = Field(default=None, description="Local start time, 24h.")
    end: dt.time | None = None
    subject: str | None = Field(default=None, description="Short subject name, e.g. 'de'.")
    subject_long: str | None = Field(default=None, description="Full subject name.")
    teachers: list[str] = Field(default_factory=list, description="Who actually teaches it.")
    rooms: list[str] = Field(default_factory=list, description="Where it actually takes place.")
    status: LessonStatus = LessonStatus.REGULAR
    status_raw: str = Field(
        default="", description="The status as WebUntis wrote it, in case `status` is 'unknown'."
    )
    kind: EntryKind = EntryKind.LESSON
    replaced_teachers: list[str] = Field(
        default_factory=list,
        description="Teachers this entry replaced — the substituted-for staff, not the cover.",
    )
    replaced_rooms: list[str] = Field(default_factory=list, description="Rooms it moved out of.")
    info: str | None = Field(
        default=None, description="Event name or lesson info, e.g. the title of a school event."
    )
    note: str | None = Field(
        default=None, description="Free-text remark from the school, e.g. a substitution note."
    )


class TimetableDay(BaseModel):
    """One day. A day with `has_data = false` is outside the school's published plan."""

    date: dt.date
    has_data: bool = True
    lessons: list[Lesson] = Field(default_factory=list)


class Timetable(BaseModel):
    """The answer of `get_timetable_day` and `get_timetable_week`."""

    profile: str = Field(description="The profile this timetable belongs to.")
    student: str | None = Field(default=None, description="The child, as the school names them.")
    start: dt.date
    end: dt.date
    days: list[TimetableDay] = Field(default_factory=list)
