"""The MCP tools.

The tool layer knows nothing about WebUntis (section 6.1): it validates arguments, resolves
a profile name, asks the client layer and returns domain objects.

Two conventions run through every data tool:

* dates are ISO-8601 on the wire, and every optional date has a documented default, because
  the model frequently calls tools without optional arguments (section 2.2),
* every result carries the profile it was produced for, so answers about two children cannot
  be confused.
"""

from __future__ import annotations

import datetime as dt
import logging

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError

from untis_mcp.config import Config, ProfileConfig
from untis_mcp.errors import ProfileNotFound, UntisMcpError
from untis_mcp.models import Absences, Appointments, ProfileSummary, Timetable
from untis_mcp.untis.client import ClientRegistry, UntisClient
from untis_mcp.untis.mapping import (
    appointments_from_calendar,
    appointments_from_timetable,
    merge_appointments,
    parse_absences,
    parse_timetable,
)

logger = logging.getLogger(__name__)


def resolve_profile(config: Config, name: str) -> ProfileConfig:
    """Resolve a `profile` argument, or fail with a message that lists the valid names.

    `ToolError` is what reaches the assistant, so the message has to be enough for it to
    retry correctly without a second round trip (FR-04).
    """
    try:
        return config.get(name)
    except ProfileNotFound as exc:
        raise ToolError(str(exc)) from exc


def parse_date(value: str | None, field: str, default: dt.date) -> dt.date:
    """Parse an ISO date argument, or explain precisely what was wrong with it."""
    if value is None or not str(value).strip():
        return default
    try:
        return dt.date.fromisoformat(str(value).strip())
    except ValueError as exc:
        raise ToolError(
            f"{field}={value!r} is not a date. Use ISO-8601, e.g. '{default.isoformat()}'."
        ) from exc


async def client_for(config: Config, registry: ClientRegistry, name: str, tool: str) -> UntisClient:
    """Resolve a profile, authenticate it, and check that it may use `tool`.

    Every failure here is turned into a `ToolError`, because these are answers the assistant
    needs to read and act on — a wrong profile name, an unreachable school, or a feature the
    account is not permitted to use (FR-06, ADR-010).
    """
    profile = resolve_profile(config, name)
    client = registry.for_profile(profile)
    try:
        await client.session()
        client.require(tool)
    except UntisMcpError as exc:
        raise ToolError(str(exc)) from exc
    return client


async def fetch_timetable(
    config: Config,
    registry: ClientRegistry,
    tool: str,
    profile: str,
    start: dt.date,
    end: dt.date,
) -> Timetable:
    """Shared body of the two timetable tools."""
    client = await client_for(config, registry, profile, tool)
    session = await client.session()
    try:
        payload = await client.timetable_entries(start, end)
    except UntisMcpError as exc:
        raise ToolError(str(exc)) from exc

    timetable = parse_timetable(
        payload,
        profile=client.profile.name,
        start=start,
        end=end,
        student=session.student_name or None,
    )
    logger.info(
        "%s(%s, %s..%s) -> %d day(s)",
        tool,
        client.profile.name,
        start,
        end,
        len(timetable.days),
    )
    return timetable


async def default_absence_range(client: UntisClient) -> tuple[dt.date, dt.date]:
    """ "Since the school year began, up to today" — the range a parent actually means.

    The school year comes from the tenant itself; if it refuses that call, a year back is a
    reasonable stand-in that still spans the whole of a normal school year.
    """
    today = dt.date.today()
    bounds = await client.school_year()
    if bounds and bounds[0] <= today:
        return bounds[0], today
    return today - dt.timedelta(days=365), today


# How far ahead "my appointments" looks when nobody says otherwise.
UPCOMING_DAYS = 28


def register_tools(mcp: FastMCP, config: Config, registry: ClientRegistry) -> None:
    """Register every tool against this configuration."""

    @mcp.tool
    def list_profiles() -> list[ProfileSummary]:
        """List the configured children and schools this server can answer questions about.

        Call this first when you do not already know which profiles exist. Every other tool
        takes one of these names as its `profile` argument. No credentials are returned.

        This never contacts the schools, so it always answers. `supported_tools` is
        therefore filled in only for profiles already used in this session.
        """
        logger.debug("list_profiles -> %d profile(s)", len(config.profiles))
        summaries = []
        for profile in config.profiles:
            capabilities = registry.cached_capabilities(profile)
            summaries.append(
                ProfileSummary(
                    name=profile.name,
                    description=profile.description,
                    supported_tools=capabilities.supported_tools if capabilities else None,
                )
            )
        return summaries

    @mcp.tool
    async def get_absences(
        profile: str,
        start: str | None = None,
        end: str | None = None,
        only_unexcused: bool = False,
    ) -> Absences:
        """Absences recorded for one child, with their reason and excuse status.

        `start` and `end` are ISO-8601 dates. They default to the whole current school year
        up to today, which is what a question like "are there unexcused absences?" means.
        Set `only_unexcused` to see just the ones the school has not accepted an excuse for.

        `unexcused_count` counts the unexcused entries in this answer; an empty list means
        nothing was recorded in the range, which is good news rather than a failure.
        """
        client = await client_for(config, registry, profile, "get_absences")
        session = await client.session()

        default_start, default_end = await default_absence_range(client)
        first = parse_date(start, "start", default_start)
        last = parse_date(end, "end", default_end)
        if last < first:
            raise ToolError(f"end={last.isoformat()} lies before start={first.isoformat()}.")

        try:
            payload = await client.absences(first, last)
        except UntisMcpError as exc:
            raise ToolError(str(exc)) from exc

        absences = parse_absences(
            payload,
            profile=client.profile.name,
            start=first,
            end=last,
            student=session.student_name or None,
        )
        if only_unexcused:
            absences.absences = [item for item in absences.absences if not item.is_excused]
        logger.info(
            "get_absences(%s, %s..%s) -> %d entries, %d unexcused",
            client.profile.name,
            first,
            last,
            len(absences.absences),
            absences.unexcused_count,
        )
        return absences

    @mcp.tool
    async def get_appointments(
        profile: str, start: str | None = None, end: str | None = None
    ) -> Appointments:
        """School events and appointments for one child — the "Meine Termine" view.

        `start` and `end` are ISO-8601 dates; by default the window runs from today four
        weeks ahead. Events come from the child's timetable, where the school enters things
        like project weeks and outings, and from the school's calendar view. Exams are not
        included: the schools do not grant parent accounts access to that endpoint.

        An empty list means nothing is scheduled in the window, not that the lookup failed.
        """
        client = await client_for(config, registry, profile, "get_appointments")
        session = await client.session()

        today = dt.date.today()
        first = parse_date(start, "start", today)
        last = parse_date(end, "end", first + dt.timedelta(days=UPCOMING_DAYS))
        if last < first:
            raise ToolError(f"end={last.isoformat()} lies before start={first.isoformat()}.")

        try:
            timetable_payload = await client.timetable_entries(first, last)
        except UntisMcpError as exc:
            raise ToolError(str(exc)) from exc

        from_timetable = appointments_from_timetable(
            parse_timetable(timetable_payload, profile=client.profile.name, start=first, end=last)
        )

        # The calendar view is the unverified half (section 5.2): it must be able to add
        # appointments, never to take the tool down.
        from_calendar = []
        try:
            from_calendar = appointments_from_calendar(await client.calendar_entries(first, last))
        except UntisMcpError as exc:
            logger.info("profile %r: calendar entries unavailable, ignoring (%s)", profile, exc)

        appointments = merge_appointments(
            profile=client.profile.name,
            start=first,
            end=last,
            from_timetable=from_timetable,
            from_calendar=from_calendar,
            student=session.student_name or None,
        )
        logger.info(
            "get_appointments(%s, %s..%s) -> %d (%d from the calendar view)",
            client.profile.name,
            first,
            last,
            len(appointments.appointments),
            len(from_calendar),
        )
        return appointments

    @mcp.tool
    async def get_timetable_day(profile: str, date: str | None = None) -> Timetable:
        """The timetable of one child for a single day.

        `date` is an ISO-8601 date and defaults to today. Each lesson states its status:
        `cancelled` lessons are dropped classes, `changed` ones are substitutions — where
        `teachers` is who actually teaches and `replaced_teachers` is who was scheduled.
        Entries of kind `event` are school events rather than lessons.

        A day with `has_data = false` lies outside the school's published plan (a weekend,
        a holiday, or simply not entered yet) — that is not an error.
        """
        day = parse_date(date, "date", dt.date.today())
        return await fetch_timetable(config, registry, "get_timetable_day", profile, day, day)

    @mcp.tool
    async def get_timetable_week(
        profile: str,
        week_of: str | None = None,
        year: int | None = None,
        week: int | None = None,
    ) -> Timetable:
        """The timetable of one child for a whole school week, Monday to Sunday.

        Give either `week_of` — any ISO-8601 date inside the week, defaulting to today — or
        an ISO calendar `year` and `week` number. Lessons carry the same status information
        as in `get_timetable_day`, so cancellations and substitutions are visible per day.
        """
        if year is not None or week is not None:
            if year is None or week is None:
                raise ToolError("give both 'year' and 'week', or neither (use 'week_of').")
            try:
                monday = dt.date.fromisocalendar(year, week, 1)
            except ValueError as exc:
                raise ToolError(f"{year}-W{week} is not a valid ISO calendar week: {exc}") from exc
        else:
            inside = parse_date(week_of, "week_of", dt.date.today())
            monday = inside - dt.timedelta(days=inside.weekday())

        return await fetch_timetable(
            config,
            registry,
            "get_timetable_week",
            profile,
            monday,
            monday + dt.timedelta(days=6),
        )
