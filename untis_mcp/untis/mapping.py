"""WebUntis payloads to domain models.

This is where the fragility lives (section 9), so the rules are:

* never raise on a payload that is merely different — drop what cannot be read and keep the
  rest, because half a timetable beats an error message,
* never assume a field's position: an element's own `type` says whether it is a teacher, a
  room or the name of an event, and the observed tenants do not agree on the ordering,
* keep the raw status alongside the normalised one, so an unfamiliar value stays visible.
"""

from __future__ import annotations

import datetime as dt
import html
import logging
import re
from typing import Any

from untis_mcp.models import (
    Absence,
    Absences,
    Appointment,
    Appointments,
    AppointmentSource,
    EntryKind,
    Lesson,
    LessonStatus,
    Message,
    MessageOfDay,
    Messages,
    MessagesOfDay,
    Timetable,
    TimetableDay,
)

logger = logging.getLogger(__name__)

# `status` values of the v1 timetable view, as observed on both tenants (section 5.2).
STATUS_MAP = {
    "REGULAR": LessonStatus.REGULAR,
    "CANCELLED": LessonStatus.CANCELLED,
    "CHANGED": LessonStatus.CHANGED,
    "ADDITIONAL": LessonStatus.ADDITIONAL,
}

KIND_MAP = {
    "NORMAL_TEACHING_PERIOD": EntryKind.LESSON,
    "EVENT": EntryKind.EVENT,
    "ADDITIONAL_PERIOD": EntryKind.ADDITIONAL,
}

MAX_POSITIONS = 8  # position1..position7, exclusive upper bound


def parse_timetable(
    payload: Any,
    profile: str,
    start: dt.date,
    end: dt.date,
    student: str | None = None,
) -> Timetable:
    """Map a `view/v1/timetable/entries` payload onto the domain model."""
    days_raw = payload.get("days") if isinstance(payload, dict) else None
    days = [day for day in (_map_day(item) for item in days_raw or []) if day is not None]
    days.sort(key=lambda day: day.date)
    return Timetable(profile=profile, student=student, start=start, end=end, days=days)


def _map_day(raw: Any) -> TimetableDay | None:
    if not isinstance(raw, dict):
        return None
    date = _parse_date(raw.get("date"))
    if date is None:
        logger.debug("skipping a day without a usable date: %r", raw.get("date"))
        return None

    entries = raw.get("gridEntries") or []
    lessons = [lesson for lesson in (_map_entry(item, date) for item in entries) if lesson]
    lessons.sort(key=lambda lesson: (lesson.start or dt.time.min, lesson.subject or ""))
    return TimetableDay(date=date, has_data=raw.get("status") != "NO_DATA", lessons=lessons)


def _map_entry(raw: Any, day: dt.date) -> Lesson | None:
    if not isinstance(raw, dict):
        return None

    start, end = _duration(raw.get("duration"))
    elements = _elements(raw)

    subject = _first(elements["current"].get("SUBJECT"))
    info = _first(elements["current"].get("INFO"))
    status_raw = str(raw.get("status") or "")

    # A cancelled or moved lesson names the reason in one of several free-text fields; the
    # first non-empty one is what a parent actually wants to read.
    note = _first_text(
        raw.get("substitutionText"),
        raw.get("lessonText"),
        raw.get("notesAll"),
        _text_of(raw.get("texts")),
    )
    detail = raw.get("statusDetail")
    if detail:
        note = f"{detail}: {note}" if note else str(detail)

    info_text = (info.long or info.short) if info else (raw.get("lessonInfo") or None)
    # The school often repeats the lesson info verbatim in the free-text fields; saying the
    # same thing twice only costs the assistant attention.
    if note and info_text and note.strip() == str(info_text).strip():
        note = None

    return Lesson(
        date=day,
        start=start,
        end=end,
        subject=subject.short if subject else None,
        subject_long=subject.long if subject else None,
        teachers=[element.short for element in elements["current"].get("TEACHER", [])],
        rooms=[element.short for element in elements["current"].get("ROOM", [])],
        status=STATUS_MAP.get(status_raw, LessonStatus.UNKNOWN),
        status_raw=status_raw,
        kind=KIND_MAP.get(str(raw.get("type") or ""), EntryKind.OTHER),
        replaced_teachers=[element.short for element in elements["removed"].get("TEACHER", [])],
        replaced_rooms=[element.short for element in elements["removed"].get("ROOM", [])],
        info=info_text,
        note=note,
    )


# ------------------------------------------------------------------------------- elements


class _Element:
    """One teacher, subject or room, as a position slot describes it."""

    __slots__ = ("short", "long")

    def __init__(self, short: str, long: str) -> None:
        self.short = short
        self.long = long


def _elements(entry: dict[str, Any]) -> dict[str, dict[str, list[_Element]]]:
    """Collect every position slot, keyed by whether it is current or removed, then by type.

    Both halves matter and either can be absent: a substitution has `current` *and*
    `removed`, while a lesson whose teacher was withdrawn without cover has `current: null`
    and only `removed` — dereferencing `current` blindly would lose exactly that case.
    """
    collected: dict[str, dict[str, list[_Element]]] = {"current": {}, "removed": {}}

    for index in range(1, MAX_POSITIONS):
        for slot in entry.get(f"position{index}") or []:
            if not isinstance(slot, dict):
                continue
            for half in ("current", "removed"):
                node = slot.get(half)
                if not isinstance(node, dict):
                    continue
                kind = str(node.get("type") or "")
                short = str(node.get("shortName") or node.get("displayName") or "").strip()
                long = str(node.get("longName") or "").strip()
                if not kind or not (short or long):
                    continue
                collected[half].setdefault(kind, []).append(_Element(short or long, long or short))

    return collected


def _first(elements: list[_Element] | None) -> _Element | None:
    return elements[0] if elements else None


# --------------------------------------------------------------------------------- values


def _parse_date(value: Any) -> dt.date | None:
    """Accept the ISO dates of the v1 view and the `yyyyMMdd` of the older endpoints."""
    if isinstance(value, dt.date):
        return value
    text = str(value or "").strip()
    if not text:
        return None
    for parse in (dt.date.fromisoformat, lambda item: dt.datetime.strptime(item, "%Y%m%d").date()):
        try:
            return parse(text)
        except ValueError:
            continue
    return None


def _duration(value: Any) -> tuple[dt.time | None, dt.time | None]:
    """`duration` holds full ISO timestamps — no time-grid lookup needed (ADR-009)."""
    if not isinstance(value, dict):
        return None, None
    return _parse_time(value.get("start")), _parse_time(value.get("end"))


def _parse_time(value: Any) -> dt.time | None:
    text = str(value or "").strip()
    if not text:
        return None
    for parse in (
        lambda item: dt.datetime.fromisoformat(item).time(),  # "2026-09-07T08:00"
        dt.time.fromisoformat,  # "08:00", should a field carry only a time
    ):
        try:
            return parse(text)
        except ValueError:
            continue
    logger.debug("unparseable timestamp %r", text)
    return None


def _text_of(texts: Any) -> str | None:
    if not isinstance(texts, list):
        return None
    parts = [
        str(item.get("text")).strip()
        for item in texts
        if isinstance(item, dict) and str(item.get("text") or "").strip()
    ]
    return " / ".join(parts) if parts else None


def _first_text(*candidates: Any) -> str | None:
    for candidate in candidates:
        text = str(candidate or "").strip()
        if text:
            return text
    return None


# ------------------------------------------------------------------------------- absences


def parse_absences(
    payload: Any,
    profile: str,
    start: dt.date,
    end: dt.date,
    student: str | None = None,
) -> Absences:
    """Map a `/api/classreg/absences/students` payload onto the domain model.

    This endpoint speaks the *older* wire format of section 5.3 — `yyyyMMdd` dates that
    arrive as numbers and unpadded `Hmm` times — so it exercises conversions the v1
    timetable view does not need at all.
    """
    data = payload.get("data") if isinstance(payload, dict) else None
    raw = data.get("absences") if isinstance(data, dict) else None
    absences = [item for item in (_map_absence(entry) for entry in raw or []) if item]
    absences.sort(key=lambda item: (item.start_date, item.start_time or dt.time.min))
    return Absences(
        profile=profile,
        student=student,
        start=start,
        end=end,
        absences=absences,
        unexcused_count=sum(1 for item in absences if not item.is_excused),
    )


def _map_absence(raw: Any) -> Absence | None:
    if not isinstance(raw, dict):
        return None
    start_date = _parse_date(raw.get("startDate"))
    if start_date is None:
        logger.debug("skipping an absence without a usable start date: %r", raw.get("startDate"))
        return None

    excuse = raw.get("excuse") if isinstance(raw.get("excuse"), dict) else {}
    status = _first_text(raw.get("excuseStatus"), excuse.get("excuseStatus"))

    return Absence(
        start_date=start_date,
        end_date=_parse_date(raw.get("endDate")) or start_date,
        start_time=_parse_clock(raw.get("startTime")),
        end_time=_parse_clock(raw.get("endTime")),
        is_excused=_is_excused(raw, excuse, status),
        excuse_status=status,
        reason=_first_text(raw.get("reason")),
        note=_first_text(raw.get("text")),
        excuse_note=_first_text(excuse.get("text")),
        recorded_by=_first_text(raw.get("createdUser"), raw.get("updatedUser")),
    )


def _is_excused(raw: dict[str, Any], excuse: dict[str, Any], status: str | None) -> bool:
    """Prefer the explicit flag; fall back to the excuse record, then to the wording.

    Guessing wrong in the *unexcused* direction is the safer error: it shows the parent
    something to check rather than quietly reporting all is well.
    """
    for candidate in (raw.get("isExcused"), excuse.get("isExcused")):
        if isinstance(candidate, bool):
            return candidate
    if status:
        return "unentschuldigt" not in status.casefold() and "entschuldigt" in status.casefold()
    return False


def _parse_clock(value: Any) -> dt.time | None:
    """Decode an unpadded `Hmm`/`HHmm` clock time: 800 is 08:00, 1600 is 16:00."""
    text = str(value if value is not None else "").strip()
    if not text or not text.isdigit():
        return None
    hour, minute = divmod(int(text), 100)
    if not (0 <= hour < 24 and 0 <= minute < 60):
        logger.debug("implausible clock value %r", value)
        return None
    return dt.time(hour, minute)


# --------------------------------------------------------------------------- appointments

# Field names the calendar endpoint might use. Unlike everything else in this module these
# are *guesses*: the endpoint answered on both tenants but returned no entries at all, all
# school year (section 5.2), so its item shape has never been observed. Reading it is
# therefore strictly additive — whatever cannot be understood is skipped, and the verified
# EVENT entries of the timetable feed are unaffected.
CALENDAR_TITLE_KEYS = ("name", "title", "subject", "text", "longName", "displayName")
CALENDAR_START_KEYS = ("startDateTime", "start", "startTime", "dateTimeFrom")
CALENDAR_END_KEYS = ("endDateTime", "end", "endTime", "dateTimeTo")
CALENDAR_NOTE_KEYS = ("notes", "note", "description", "remark", "text")


def appointments_from_timetable(timetable: Timetable) -> list[Appointment]:
    """The verified source: `EVENT` entries of the timetable feed (ADR-009)."""
    appointments = []
    for day in timetable.days:
        for lesson in day.lessons:
            if lesson.kind is not EntryKind.EVENT:
                continue
            title = _first_text(lesson.info, lesson.subject_long, lesson.subject, lesson.note)
            start, end, all_day = _day_span(lesson.start, lesson.end)
            appointments.append(
                Appointment(
                    date=lesson.date,
                    start=start,
                    end=end,
                    all_day=all_day,
                    title=title,
                    teachers=lesson.teachers,
                    rooms=lesson.rooms,
                    note=lesson.note if lesson.note != title else None,
                    is_cancelled=lesson.status is LessonStatus.CANCELLED,
                    source=AppointmentSource.TIMETABLE,
                )
            )
    return appointments


DAY_START = dt.time(0, 0)
DAY_END = dt.time(23, 59)


def _day_span(
    start: dt.time | None, end: dt.time | None
) -> tuple[dt.time | None, dt.time | None, bool]:
    """Strip the day boundaries a multi-day event is sliced along.

    A school event spanning several days arrives as one entry per day, and the observed
    shape is 08:00-23:59, then 00:00-23:59, then 00:00-16:35. The 00:00 and 23:59 are
    continuation markers rather than times anybody agreed on, so they are dropped: the first
    slice keeps "from 08:00", the last keeps "until 16:35", and a slice that is only
    boundaries is simply an all-day event. Reporting them literally invites the assistant to
    announce an appointment at midnight.
    """
    all_day = start == DAY_START and end is not None and end >= DAY_END
    return (
        None if start == DAY_START else start,
        None if end is not None and end >= DAY_END else end,
        all_day,
    )


def appointments_from_calendar(payload: Any) -> list[Appointment]:
    """The unverified source: `calendar-entry/detail`.

    Written to survive a shape nobody has seen: an entry with no readable date or title is
    dropped rather than guessed at, because a wrong appointment is worse than a missing one.
    """
    entries = payload.get("calendarEntries") if isinstance(payload, dict) else None
    appointments = []
    for entry in entries or []:
        if not isinstance(entry, dict):
            continue
        start_raw = _pick(entry, CALENDAR_START_KEYS)
        date = _parse_date(str(start_raw).split("T")[0]) if start_raw else None
        if date is None:
            logger.debug("skipping a calendar entry without a readable date: %r", entry)
            continue
        title = _first_text(*(entry.get(key) for key in CALENDAR_TITLE_KEYS))
        if not title:
            logger.debug("skipping a calendar entry without a readable title: %r", entry)
            continue
        start, end, all_day = _day_span(
            _parse_time(start_raw), _parse_time(_pick(entry, CALENDAR_END_KEYS))
        )
        appointments.append(
            Appointment(
                date=date,
                start=start,
                end=end,
                all_day=all_day,
                title=title,
                note=_first_text(*(entry.get(key) for key in CALENDAR_NOTE_KEYS)),
                source=AppointmentSource.CALENDAR,
            )
        )
    return appointments


def merge_appointments(
    profile: str,
    start: dt.date,
    end: dt.date,
    from_timetable: list[Appointment],
    from_calendar: list[Appointment],
    student: str | None = None,
) -> Appointments:
    """Combine both sources, dropping calendar entries the timetable already reported."""

    def key(item: Appointment) -> tuple[dt.date, str]:
        return item.date, (item.title or "").casefold()

    seen = {key(item) for item in from_timetable}
    merged = list(from_timetable)
    for item in from_calendar:
        if key(item) in seen:
            continue
        seen.add(key(item))
        merged.append(item)

    merged.sort(key=lambda item: (item.date, item.start or dt.time.min, item.title or ""))
    return Appointments(profile=profile, student=student, start=start, end=end, appointments=merged)


def _pick(entry: dict[str, Any], keys: tuple[str, ...]) -> Any:
    for key in keys:
        if entry.get(key):
            return entry[key]
    return None


# ------------------------------------------------------------------------------- messages

# The school writes announcements in a rich-text box, so the payload contains HTML. An
# assistant reading "<br>" aloud helps nobody, and neither does an escaped umlaut.
BREAKS = re.compile(r"(?i)<br\s*/?>|</p\s*>|</div\s*>|</li\s*>")
TAGS = re.compile(r"<[^>]+>")


def plain_text(value: Any) -> str | None:
    """Turn a school's HTML snippet into readable text, preserving its line breaks."""
    text = str(value or "")
    if not text.strip():
        return None
    text = BREAKS.sub("\n", text)
    text = TAGS.sub("", text)
    text = html.unescape(text)
    lines = [line.strip() for line in text.splitlines()]
    cleaned = "\n".join(line for line in lines if line)
    return cleaned or None


def parse_messages_of_day(payload: Any, profile: str, date: dt.date) -> MessagesOfDay:
    """Map a `newsWidgetData` payload. Verified against a tenant that publishes these."""
    data = payload.get("data") if isinstance(payload, dict) else None
    data = data if isinstance(data, dict) else {}

    messages = []
    for entry in data.get("messagesOfDay") or []:
        if not isinstance(entry, dict):
            continue
        text = plain_text(entry.get("text"))
        subject = plain_text(entry.get("subject"))
        if not text and not subject:
            continue
        messages.append(
            MessageOfDay(
                subject=subject,
                text=text or subject or "",
                has_attachments=bool(entry.get("attachments")),
            )
        )

    return MessagesOfDay(
        profile=profile,
        date=date,
        messages=messages,
        system_message=plain_text(data.get("systemMessage")),
    )


# The inbox envelope is verified, but both of its lists were empty on the tenant that grants
# access, so the *item* shape has never been observed. These are the plausible namings; an
# item that yields neither a subject nor a body is skipped rather than reported as blank.
MESSAGE_SUBJECT_KEYS = ("subject", "title", "name")
MESSAGE_BODY_KEYS = ("content", "body", "text", "messageContent", "contentPreview")
MESSAGE_SENDER_KEYS = ("sender", "from", "senderName", "author")
MESSAGE_SENT_KEYS = ("sentDateTime", "sentDate", "sentAt", "date", "timestamp")
MESSAGE_READ_KEYS = ("isRead", "read")


def parse_messages(payload: Any, profile: str) -> Messages:
    """Map a `view/v1/messages` payload onto the domain model."""
    envelope = payload if isinstance(payload, dict) else {}
    messages: list[Message] = []
    for key, needs_confirmation in (
        ("incomingMessages", False),
        ("readConfirmationMessages", True),
    ):
        for entry in envelope.get(key) or []:
            message = _map_message(entry, needs_confirmation)
            if message:
                messages.append(message)

    messages.sort(key=lambda item: (item.sent is None, item.sent or dt.datetime.min), reverse=True)
    return Messages(
        profile=profile,
        messages=messages,
        unread_count=sum(1 for item in messages if item.is_read is False),
    )


def _map_message(raw: Any, needs_confirmation: bool) -> Message | None:
    if not isinstance(raw, dict):
        return None
    subject = plain_text(_pick(raw, MESSAGE_SUBJECT_KEYS))
    body = plain_text(_pick(raw, MESSAGE_BODY_KEYS))
    if not subject and not body:
        logger.debug("skipping a message with neither subject nor body: %r", raw)
        return None

    read = next(
        (raw[key] for key in MESSAGE_READ_KEYS if isinstance(raw.get(key), bool)),
        None,
    )
    return Message(
        subject=subject,
        body=body,
        sender=_sender(_pick(raw, MESSAGE_SENDER_KEYS)),
        sent=_parse_datetime(_pick(raw, MESSAGE_SENT_KEYS)),
        is_read=read,
        needs_confirmation=needs_confirmation,
        has_attachments=bool(raw.get("attachments")),
    )


def _sender(value: Any) -> str | None:
    """A sender may be a plain string or an object naming a person."""
    if isinstance(value, dict):
        return _first_text(
            value.get("displayName"),
            value.get("name"),
            value.get("longName"),
            value.get("userName"),
        )
    return _first_text(value)


def _parse_datetime(value: Any) -> dt.datetime | None:
    if isinstance(value, int | float) and value > 0:
        # Epoch milliseconds, as createDate and lastUpdate use elsewhere (section 5.3).
        try:
            return dt.datetime.fromtimestamp(value / 1000)
        except (OverflowError, OSError, ValueError):
            return None
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return dt.datetime.fromisoformat(text)
    except ValueError:
        date = _parse_date(text)
        return dt.datetime.combine(date, dt.time.min) if date else None
