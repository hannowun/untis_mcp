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


class Absence(BaseModel):
    """One recorded absence, with the school's own reason and excuse state."""

    start_date: dt.date
    end_date: dt.date
    start_time: dt.time | None = Field(default=None, description="Local start time, 24h.")
    end_time: dt.time | None = None
    is_excused: bool = Field(
        description="Whether the school has accepted an excuse for this absence."
    )
    excuse_status: str | None = Field(
        default=None,
        description="The school's own wording for the excuse state, e.g. 'entschuldigt'.",
    )
    reason: str | None = Field(
        default=None, description="Why the child was absent, as the school recorded it."
    )
    note: str | None = Field(default=None, description="Free text attached to the absence.")
    excuse_note: str | None = Field(default=None, description="Free text attached to the excuse.")
    recorded_by: str | None = Field(
        default=None, description="Who entered it — usually a teacher or the school office."
    )


class Absences(BaseModel):
    """The answer of `get_absences`."""

    profile: str = Field(description="The profile these absences belong to.")
    student: str | None = Field(default=None, description="The child, as the school names them.")
    start: dt.date
    end: dt.date
    absences: list[Absence] = Field(default_factory=list)
    unexcused_count: int = Field(
        default=0,
        description="How many of the returned absences are not excused — count them from here "
        "rather than by re-reading the list.",
    )


class AppointmentSource(StrEnum):
    """Where an appointment was found. See ADR-009 for why there are two sources."""

    TIMETABLE = "timetable"
    CALENDAR = "calendar"

    @classmethod
    def _missing_(cls, value: object) -> AppointmentSource:
        return cls.TIMETABLE


class Appointment(BaseModel):
    """A school event or calendar entry — the "Meine Termine" view of WebUntis."""

    date: dt.date
    start: dt.time | None = None
    end: dt.time | None = None
    title: str | None = Field(
        default=None,
        description=(
            "What the event is called. Null when the school entered no name at all — one of "
            "the two schools routinely does that — in which case describe the appointment by "
            "its time, teachers and room instead of inventing a title."
        ),
    )
    all_day: bool = Field(
        default=False, description="True when the event covers the whole day rather than a slot."
    )
    # start/end are null on the days a multi-day event merely continues through, so
    # "start = null, end = 16:35" means "until 16:35 on this day".
    teachers: list[str] = Field(default_factory=list)
    rooms: list[str] = Field(default_factory=list)
    note: str | None = None
    is_cancelled: bool = Field(
        default=False, description="Whether the school marked this event as cancelled."
    )
    source: AppointmentSource = Field(
        default=AppointmentSource.TIMETABLE,
        description="Which WebUntis view this came from; both are read and merged.",
    )


class Appointments(BaseModel):
    """The answer of `get_appointments`."""

    profile: str = Field(description="The profile these appointments belong to.")
    student: str | None = Field(default=None, description="The child, as the school names them.")
    start: dt.date
    end: dt.date
    appointments: list[Appointment] = Field(default_factory=list)
