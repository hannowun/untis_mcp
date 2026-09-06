#!/usr/bin/env python
"""Protocol spike - step 0 of the implementation order.

Logs into every configured WebUntis tenant with the real parent credentials, walks the
endpoints listed in section 5.2 of ``documents/requirements-and-design.md``, and writes the
raw responses to disk.

Its purpose is to answer open questions 1-4 and 6 of section 10 before any parsing code is
written, and to produce the payloads that later become anonymised test fixtures.

Two things a parent login makes non-obvious, both learned from the first run:

* The ``personId`` returned by ``authenticate`` is the *parent*, and every element-scoped
  endpoint rejects it. The child's id comes from ``app/data`` as ``user.students``.
* The speculative endpoints answer HTTP 500 with the name of the parameter they are missing.
  That turns them into an oracle: ``discover_parameters`` follows the complaints until the
  endpoint is satisfied or stops naming a parameter we can supply.

This script is deliberately self-contained: it does not import ``untis_mcp``, because the
client layer it would import does not exist yet and its design depends on these results.

Usage::

    uv run python scripts/probe_endpoints.py                  # all profiles
    uv run python scripts/probe_endpoints.py --profile anna   # one profile
    uv run python scripts/probe_endpoints.py --start 2026-09-01 --end 2026-09-30

Output goes to ``probe-output/<run>/`` (git-ignored). It contains real names and timetables -
anonymise before turning anything into a test fixture.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import contextlib
import json
import os
import re
import sys
import time
import tomllib
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx

CLIENT_ID = "untis-mcp-probe"
REPO_ROOT = Path(__file__).resolve().parent.parent

# Keys whose values must never reach the output files, however deeply nested.
SECRET_KEYS = {
    "jsessionid",
    "sessionid",
    "session_id",
    "token",
    "jwt",
    "accesstoken",
    "access_token",
    "refreshtoken",
    "refresh_token",
    "password",
    "authorization",
    "bearer",
    "secret",
}


# --------------------------------------------------------------------------- configuration


@dataclass(frozen=True)
class Profile:
    name: str
    description: str
    server: str
    school: str
    username: str
    password: str


def load_profiles(path: Path) -> list[Profile]:
    """Read the same TOML file the server will use (ADR-003)."""
    if not path.is_file():
        sys.exit(
            f"configuration not found: {path}\n"
            f"copy config/profiles.example.toml to {path} and fill in real credentials"
        )

    with path.open("rb") as handle:
        raw = tomllib.load(handle)

    entries = raw.get("profiles")
    if not entries:
        sys.exit(f"{path} contains no [[profiles]] entries")

    profiles: list[Profile] = []
    for index, entry in enumerate(entries):
        missing = [key for key in ("name", "server", "school", "username") if not entry.get(key)]
        if missing:
            sys.exit(f"profile #{index + 1} in {path} is missing: {', '.join(missing)}")

        password = entry.get("password")
        env_name = entry.get("password_env")
        if env_name:
            password = os.environ.get(env_name)
            if not password:
                sys.exit(
                    f"profile '{entry['name']}': password_env is set to '{env_name}', "
                    f"but that environment variable is empty"
                )
        if not password:
            sys.exit(f"profile '{entry['name']}': neither password nor password_env is set")

        profiles.append(
            Profile(
                name=entry["name"],
                description=entry.get("description", ""),
                server=str(entry["server"]).rstrip("/"),
                school=entry["school"],
                username=entry["username"],
                password=password,
            )
        )
    return profiles


# ------------------------------------------------------------------------------- redaction


def redact(value: Any) -> Any:
    """Recursively replace secret-looking values, so output files stay less dangerous."""
    if isinstance(value, dict):
        return {
            key: ("<redacted>" if str(key).lower() in SECRET_KEYS else redact(item))
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact(item) for item in value]
    return value


def _lists_in(value: Any):
    """Yield every list found anywhere in a payload."""
    if isinstance(value, dict):
        for item in value.values():
            yield from _lists_in(item)
    elif isinstance(value, list):
        yield value
        for item in value:
            yield from _lists_in(item)


def payload_is_empty(payload: Any) -> bool:
    """True when the endpoint answered but carried no records.

    Telling "works, but nothing in this date range" apart from "does not work for this
    account" is the whole point of the spike, so this must not be fooled by the envelope
    a payload arrives in: some endpoints wrap their records in ``data``, some in a named
    key such as ``incomingMessages``, and JSON-RPC returns a bare list.
    """
    lists = list(_lists_in(payload))
    if lists:
        return all(not item for item in lists)
    return not payload


def sketch(value: Any, depth: int = 3) -> Any:
    """A compact type outline of a payload - the part that informs the domain model."""
    if isinstance(value, dict):
        if depth <= 0:
            return f"<dict, {len(value)} keys>"
        return {key: sketch(item, depth - 1) for key, item in list(value.items())[:30]}
    if isinstance(value, list):
        if not value:
            return "<empty list>"
        if depth <= 0:
            return f"<list, {len(value)} items>"
        return {f"<list, {len(value)} items>": sketch(value[0], depth - 1)}
    if value is None:
        return "null"
    if isinstance(value, str):
        return f"str (e.g. {value[:40]!r})" if value else "str (empty)"
    return type(value).__name__


def find_key(payload: Any, wanted: str) -> Any:
    """Depth-first search for a key, because payload shapes differ per WebUntis version."""
    if isinstance(payload, dict):
        for key, value in payload.items():
            if key == wanted:
                return value
            found = find_key(value, wanted)
            if found is not None:
                return found
    elif isinstance(payload, list):
        for item in payload:
            found = find_key(item, wanted)
            if found is not None:
                return found
    return None


def parse_compact(value: Any) -> date | None:
    """Decode a WebUntis ``yyyyMMdd`` date, which arrives as a number or a string."""
    text = str(value)
    try:
        return datetime.strptime(text, "%Y%m%d").date()
    except (TypeError, ValueError):
        return None


def clamp_to_schoolyear(start: date, end: date, schoolyear: Any) -> tuple[date, date]:
    """Keep a range inside the current school year.

    JSON-RPC ``getTimetable`` answers -8507 ("startDate and endDate are not within a single
    school year") otherwise, which is how one of the two tenants failed on the first run
    while the other, whose school year started earlier, did not.
    """
    if not isinstance(schoolyear, dict):
        return start, end
    year_start = parse_compact(schoolyear.get("startDate"))
    year_end = parse_compact(schoolyear.get("endDate"))
    if year_start:
        start = max(start, year_start)
    if year_end:
        end = min(end, year_end)
    return start, (end if end >= start else start)


def find_klasse_id(payload: Any) -> int | None:
    """Pull the class id out of a timetable payload (element type 1 per section 5.3).

    ``authenticate`` reports ``klasseId: 0`` for a parent, but ``/api/exams`` insists on a
    real one, so it has to be recovered from the timetable the child is actually in.
    """
    if isinstance(payload, dict):
        if payload.get("type") == 1 and isinstance(payload.get("id"), int):
            return payload["id"]
        for value in payload.values():
            found = find_klasse_id(value)
            if found:
                return found
    elif isinstance(payload, list):
        for item in payload:
            found = find_klasse_id(item)
            if found:
                return found
    return None


# ------------------------------------------------------------------------------ probe spec


@dataclass
class Probe:
    """One request to try. ``kind`` is 'rest' or 'rpc'."""

    name: str
    kind: str
    path: str
    params: dict[str, Any] = field(default_factory=dict)
    auth: str = "cookie"  # cookie | jwt | jwt+tenant
    confidence: str = "documented"  # documented | speculative
    note: str = ""
    discover: bool = False  # let the server's complaints fill in the missing parameters


@dataclass
class AuthContext:
    session_id: str
    person_type: int | None
    person_id: int | None
    klasse_id: int | None
    jwt: str | None = None
    tenant_id: Any = None
    app_data: Any = None
    students: list[dict[str, Any]] = field(default_factory=list)
    roles: list[str] = field(default_factory=list)
    permissions: list[str] = field(default_factory=list)
    views: list[str] = field(default_factory=list)


def build_probes(
    ctx: AuthContext,
    student_id: int,
    klasse_id: int | None,
    start: date,
    end: date,
    rpc_range: tuple[date, date] | None = None,
) -> list[Probe]:
    """The endpoint map of section 5.2, scoped to the *child*, plus a few guesses.

    ``student_id`` is deliberately a parameter rather than ``ctx.person_id``: for a parent
    account those are two different people, and passing the parent is what produced a wall
    of 403s in the first run.
    """
    compact_start = start.strftime("%Y%m%d")
    compact_end = end.strftime("%Y%m%d")
    # JSON-RPC insists on a range inside one school year; REST does not care.
    rpc_start, rpc_end = rpc_range or (start, end)
    # The Monday of the *current* week, not of the range start: the first run asked for a
    # holiday week, got an empty payload, and so never found the class id in it.
    today = max(start, min(end, date.today()))
    monday = today - timedelta(days=today.weekday())

    return [
        # --- section 5.2, documented ------------------------------------------------
        Probe(
            "timetable-weekly-data",
            "rest",
            "/WebUntis/api/public/timetable/weekly/data",
            {
                "elementType": 5,
                "elementId": student_id,
                "date": monday.isoformat(),
                "formatId": 1,
            },
            note="primary timetable source (tools 3+4); note the ISO date, unlike the rest",
        ),
        Probe(
            "timetable-jsonrpc",
            "rpc",
            "getTimetable",
            {
                "id": student_id,
                "type": 5,
                "startDate": int(rpc_start.strftime("%Y%m%d")),
                "endDate": int(rpc_end.strftime("%Y%m%d")),
            },
            note="fallback timetable source, and cross-check for the weekly view",
        ),
        Probe(
            "absences",
            "rest",
            "/WebUntis/api/classreg/absences/students",
            {
                "startDate": compact_start,
                "endDate": compact_end,
                "studentId": student_id,
                "excuseStatusId": -1,
            },
            note="tool 2; open question 3",
        ),
        Probe(
            "exams",
            "rest",
            "/WebUntis/api/exams",
            {
                "startDate": compact_start,
                "endDate": compact_end,
                "klasseId": klasse_id or 0,
                "withGrades": "true",
            },
            note="the confirmed half of tool 1; klasseId is discovered from the timetable",
        ),
        Probe(
            "messages-inbox",
            "rest",
            "/WebUntis/api/rest/view/v1/messages",
            auth="jwt+tenant",
            note="tool 5; needs the JWT and the tenant-id header",
        ),
        Probe(
            "news-widget",
            "rest",
            "/WebUntis/api/public/news/newsWidgetData",
            {"date": compact_start},
            note="messages of the day, the cookie-only alternative for tool 5",
        ),
        # --- capability and master data, cheap and informative ------------------------
        Probe(
            "schoolyear-current",
            "rpc",
            "getCurrentSchoolyear",
            note="school year bounds; also a cheap check of what JSON-RPC allows a parent",
        ),
        Probe(
            "timegrid-units",
            "rpc",
            "getTimegridUnits",
            note="period number -> clock time, needed to render a readable timetable",
        ),
        Probe(
            "holidays",
            "rpc",
            "getHolidays",
            note="lets the tools explain an empty timetable instead of returning nothing",
        ),
        # --- open question 1: the calendar-entry endpoint ------------------------------
        # It exists on both tenants; the first run only failed parameter validation, so
        # these are run in discovery mode and let the server name what it wants.
        Probe(
            "calendar-entry-detail-v2",
            "rest",
            "/WebUntis/api/rest/view/v2/calendar-entry/detail",
            {"elementId": student_id, "elementType": 5},
            auth="jwt+tenant",
            confidence="speculative",
            discover=True,
            note="THE open question: what does 'Meine Termine' need and return?",
        ),
        Probe(
            "timetable-entries-v1",
            "rest",
            "/WebUntis/api/rest/view/v1/timetable/entries",
            {
                "start": start.isoformat(),
                "end": end.isoformat(),
                "format": 1,
                "resourceType": "STUDENT",
                "resources": student_id,
            },
            auth="jwt+tenant",
            confidence="speculative",
            discover=True,
            note="newer timetable view; wants resourceType rather than elementType",
        ),
    ]


# --------------------------------------------------------------------- parameter discovery

MISSING_PARAM = re.compile(
    r"Required request parameter '([^']+)' for method parameter type (\w+) is not present"
)
# "The parameter value 'STUDENT' is no valid value for the expected type: 'class java.lang.Long'"
# names the offending *value*, not the parameter, so the parameter has to be looked up by it.
BAD_VALUE = re.compile(
    r"parameter value '([^']*)' is no valid value for the expected type: '(?:class )?([\w.]+)'"
)


def parameter_candidates(name: str, type_name: str, student_id: int, start: date, end: date):
    """Plausible values for a parameter the server just asked for, best guess first."""
    lowered = name.lower()
    # resourceType wants the enum name (timetable/entries accepted "STUDENT"), while
    # elementType is a Long (calendar-entry rejected "STUDENT" as no valid Long).
    if "resourcetype" in lowered:
        return ["STUDENT", 5]
    if "elementtype" in lowered or lowered == "type":
        return [5, "STUDENT"]
    if "elementid" in lowered or "resource" in lowered or lowered in {"id", "studentid"}:
        return [student_id]
    if "start" in lowered:
        return [start.isoformat(), f"{start.isoformat()}T00:00", start.strftime("%Y%m%d")]
    if "end" in lowered:
        return [end.isoformat(), f"{end.isoformat()}T23:59", end.strftime("%Y%m%d")]
    if "date" in lowered:
        return [start.isoformat(), start.strftime("%Y%m%d")]
    if type_name in {"Boolean", "boolean"}:
        return ["false", "true"]
    if type_name in {"Integer", "Long", "int", "long"}:
        return [1]
    return []


async def discover_parameters(
    client: httpx.AsyncClient,
    profile: Profile,
    probe: Probe,
    headers: dict[str, str],
    student_id: int,
    start: date,
    end: date,
    max_rounds: int = 10,
) -> tuple[httpx.Response, dict[str, Any], list[str]]:
    """Call an endpoint repeatedly, letting its own error messages complete the query.

    WebUntis answers a malformed request with the name and Java type of the parameter it
    wanted. Following that chain discovers a working parameter set without guessing blindly,
    and the trail itself is the documentation this spike is meant to produce.
    """
    params: dict[str, Any] = dict(probe.params)
    tried: dict[str, int] = {}
    trail: list[str] = []

    response = await client.get(f"{profile.server}{probe.path}", params=params, headers=headers)
    for _ in range(max_rounds):
        if response.status_code < 400:
            break

        message = ""
        with contextlib.suppress(ValueError):
            body = response.json()
            message = str(body.get("errorMessage") or body.get("errors") or "")
        if not message:
            message = response.text[:300]

        missing = MISSING_PARAM.search(message)
        bad = BAD_VALUE.search(message)
        if missing:
            name, type_name = missing.group(1), missing.group(2)
        elif bad:
            rejected, type_name = bad.group(1), bad.group(2)
            name = next((key for key, value in params.items() if str(value) == rejected), "")
            if not name:
                trail.append(f"rejected value {rejected!r} belongs to no parameter we sent")
                break
        else:
            break

        index = tried.get(name, 0) if bad else 0
        candidates = parameter_candidates(name, type_name, student_id, start, end)
        if index >= len(candidates):
            trail.append(f"{name}: out of candidates")
            break

        params[name] = candidates[index]
        tried[name] = index + 1
        trail.append(f"{name}={candidates[index]!r}")
        response = await client.get(f"{profile.server}{probe.path}", params=params, headers=headers)

    return response, params, trail


# ------------------------------------------------------------------------------ execution


class ProbeError(Exception):
    """A WebUntis-level refusal, as opposed to a transport failure."""


async def json_rpc(client: httpx.AsyncClient, profile: Profile, method: str, params: Any) -> Any:
    response = await client.post(
        f"{profile.server}/WebUntis/jsonrpc.do",
        params={"school": profile.school},
        json={"id": CLIENT_ID, "method": method, "params": params, "jsonrpc": "2.0"},
    )
    response.raise_for_status()
    payload = response.json()
    if "error" in payload:
        error = payload["error"]
        raise ProbeError(f"JSON-RPC error {error.get('code')}: {error.get('message')}")
    return payload.get("result")


async def authenticate(client: httpx.AsyncClient, profile: Profile) -> AuthContext:
    """Section 5.1, steps 1-4."""
    result = await json_rpc(
        client,
        profile,
        "authenticate",
        {"user": profile.username, "password": profile.password, "client": CLIENT_ID},
    )
    if not result or not result.get("sessionId"):
        raise ProbeError("authenticate returned no sessionId")

    ctx = AuthContext(
        session_id=result["sessionId"],
        person_type=result.get("personType"),
        person_id=result.get("personId"),
        klasse_id=result.get("klasseId"),
    )

    host = httpx.URL(profile.server).host
    encoded_school = "_" + base64.b64encode(profile.school.encode()).decode()
    client.cookies.set("JSESSIONID", ctx.session_id, domain=host, path="/")
    client.cookies.set("schoolname", encoded_school, domain=host, path="/")

    # Step 3: the bearer token, returned as a plain string rather than JSON.
    try:
        token_response = await client.get(f"{profile.server}/WebUntis/api/token/new")
        if token_response.status_code == 200 and token_response.text.strip():
            ctx.jwt = token_response.text.strip()
    except httpx.HTTPError as exc:
        print(f"    ! token/new failed: {type(exc).__name__}", file=sys.stderr)

    # Step 4: app/data carries the tenant id, the children this login may see, and the
    # permission list - i.e. everything the tools need to know before asking anything.
    if ctx.jwt:
        try:
            app_response = await client.get(
                f"{profile.server}/WebUntis/api/rest/view/v1/app/data",
                headers={"Authorization": f"Bearer {ctx.jwt}"},
            )
            if app_response.status_code == 200:
                ctx.app_data = app_response.json()
                read_app_data(ctx)
        except (httpx.HTTPError, ValueError) as exc:
            print(f"    ! app/data failed: {type(exc).__name__}", file=sys.stderr)

    return ctx


def read_app_data(ctx: AuthContext) -> None:
    """Extract the parts of app/data the rest of the run depends on."""
    data = ctx.app_data if isinstance(ctx.app_data, dict) else {}
    tenant = data.get("tenant") or {}
    # The first run looked for "tenantId" and found nothing: it lives at tenant.id.
    ctx.tenant_id = tenant.get("id") or find_key(data, "tenantId")
    user = data.get("user") or {}
    ctx.students = [item for item in (user.get("students") or []) if isinstance(item, dict)]
    ctx.roles = list(user.get("roles") or [])
    ctx.permissions = [item for item in (data.get("permissions") or []) if isinstance(item, str)]
    ctx.views = list((user.get("permissions") or {}).get("views") or [])


def headers_for(probe: Probe, ctx: AuthContext) -> dict[str, str]:
    headers: dict[str, str] = {}
    if probe.auth in {"jwt", "jwt+tenant"} and ctx.jwt:
        headers["Authorization"] = f"Bearer {ctx.jwt}"
    if probe.auth == "jwt+tenant" and ctx.tenant_id is not None:
        headers["tenant-id"] = str(ctx.tenant_id)
    return headers


async def run_probe(
    client: httpx.AsyncClient,
    profile: Profile,
    ctx: AuthContext,
    probe: Probe,
    out_dir: Path,
    student_id: int = 0,
    start: date | None = None,
    end: date | None = None,
) -> tuple[dict[str, Any], Any]:
    """Run one probe. Returns the record for the report and the payload, if any."""
    record: dict[str, Any] = {
        "name": probe.name,
        "kind": probe.kind,
        "target": probe.path,
        "params": probe.params,
        "auth": probe.auth,
        "confidence": probe.confidence,
        "note": probe.note,
    }

    started = time.perf_counter()
    try:
        if probe.kind == "rpc":
            payload = await json_rpc(client, profile, probe.path, probe.params)
            record["http_status"] = 200
            record["elapsed_ms"] = round((time.perf_counter() - started) * 1000)
        else:
            if probe.discover and start and end:
                response, params, trail = await discover_parameters(
                    client, profile, probe, headers_for(probe, ctx), student_id, start, end
                )
                record["params"] = params
                record["discovery_trail"] = trail
            else:
                response = await client.get(
                    f"{profile.server}{probe.path}",
                    params=probe.params,
                    headers=headers_for(probe, ctx),
                )
            record["http_status"] = response.status_code
            record["content_type"] = response.headers.get("content-type", "")
            record["elapsed_ms"] = round((time.perf_counter() - started) * 1000)
            if response.status_code >= 400:
                record["verdict"] = f"http {response.status_code}"
                record["body_excerpt"] = response.text[:400]
                return record, None
            try:
                payload = response.json()
            except ValueError:
                record["verdict"] = "ok (non-JSON)"
                record["body_excerpt"] = response.text[:400]
                return record, None
    except ProbeError as exc:
        record["verdict"] = f"denied ({exc})"
        return record, None
    except httpx.HTTPError as exc:
        record["verdict"] = f"transport error ({type(exc).__name__})"
        return record, None

    # A WebUntis REST error is frequently a 200 carrying an error envelope.
    if isinstance(payload, dict) and payload.get("error"):
        record["verdict"] = f"error envelope: {str(payload['error'])[:120]}"
        return record, None

    record["verdict"] = "ok (empty)" if payload_is_empty(payload) else "ok"
    record["items"] = sum(len(item) for item in _lists_in(payload))
    record["shape"] = sketch(payload)

    body_file = out_dir / f"{probe.name}.json"
    body_file.write_text(
        json.dumps(redact(payload), indent=2, ensure_ascii=False, default=str),
        encoding="utf-8",
    )
    record["body_file"] = body_file.name
    return record, payload


async def probe_profile(profile: Profile, args: argparse.Namespace, run_dir: Path) -> dict:
    out_dir = run_dir / profile.name
    out_dir.mkdir(parents=True, exist_ok=True)
    summary: dict[str, Any] = {"profile": profile.name, "server": profile.server}

    print(f"\n=== {profile.name} ({profile.description or profile.server}) ===")

    async with httpx.AsyncClient(timeout=args.timeout, follow_redirects=True) as client:
        try:
            ctx = await authenticate(client, profile)
        except (ProbeError, httpx.HTTPError) as exc:
            print(f"    login FAILED: {exc}")
            summary["login"] = f"failed: {exc}"
            (out_dir / "_summary.json").write_text(
                json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8"
            )
            return summary

        if ctx.app_data is not None:
            (out_dir / "app-data.json").write_text(
                json.dumps(redact(ctx.app_data), indent=2, ensure_ascii=False),
                encoding="utf-8",
            )

        student_id = args.student_id or (ctx.students[0]["id"] if ctx.students else ctx.person_id)
        print(
            f"    login ok - roles={ctx.roles or '?'} parentPersonId={ctx.person_id} "
            f"tenantId={ctx.tenant_id or 'NOT FOUND'}"
        )
        print(
            f"    students={[(s.get('id'), s.get('displayName')) for s in ctx.students]} "
            f"-> probing as studentId={student_id}"
        )

        summary.update(
            login="ok",
            roles=ctx.roles,
            parent_person_id=ctx.person_id,
            person_type=ctx.person_type,
            tenant_id=ctx.tenant_id,
            students=[{"id": s.get("id"), "name": s.get("displayName")} for s in ctx.students],
            student_id_used=student_id,
            permissions=ctx.permissions,
            views=ctx.views,
        )
        if len(ctx.students) > 1:
            print(f"    ! {len(ctx.students)} children on this login; probing the first only")

        # Pre-flight: the school year bounds decide what JSON-RPC will accept.
        schoolyear = None
        with contextlib.suppress(ProbeError, httpx.HTTPError):
            schoolyear = await json_rpc(client, profile, "getCurrentSchoolyear", {})
        rpc_range = clamp_to_schoolyear(args.start, args.end, schoolyear)
        if rpc_range != (args.start, args.end):
            print(
                f"    JSON-RPC range clamped to the school year: {rpc_range[0]} .. {rpc_range[1]}"
            )
        summary["schoolyear"] = schoolyear
        summary["rpc_range"] = [str(rpc_range[0]), str(rpc_range[1])]

        probes = build_probes(ctx, student_id, None, args.start, args.end, rpc_range)
        if args.only:
            wanted = set(args.only)
            unknown = wanted - {item.name for item in probes}
            if unknown:
                sys.exit(f"unknown probe(s): {', '.join(sorted(unknown))}")
            probes = [item for item in probes if item.name in wanted]
        records = []
        for probe in probes:
            record, payload = await run_probe(
                client, profile, ctx, probe, out_dir, student_id, args.start, args.end
            )
            marker = "  " if str(record["verdict"]).startswith("ok") else "!!"
            flag = "?" if probe.confidence == "speculative" else " "
            items = f" ({record['items']} items)" if record.get("items") else ""
            print(f" {marker}{flag} {probe.name:<32} {record['verdict']}{items}")
            records.append(record)

            # The exams endpoint needs a real class id, which only the timetable knows.
            if probe.name == "timetable-weekly-data" and payload is not None:
                klasse_id = find_klasse_id(payload)
                if klasse_id:
                    summary["klasse_id"] = klasse_id
                    for pending in probes:
                        if pending.name == "exams":
                            pending.params["klasseId"] = klasse_id
                    print(f"       discovered klasseId={klasse_id} for the exams probe")

        summary["probes"] = records

        with contextlib.suppress(ProbeError, httpx.HTTPError):
            await json_rpc(client, profile, "logout", {})

    (out_dir / "_summary.json").write_text(
        json.dumps(summary, indent=2, ensure_ascii=False, default=str), encoding="utf-8"
    )
    return summary


# --------------------------------------------------------------------------------- report


def write_report(summaries: list[dict], run_dir: Path, args: argparse.Namespace) -> Path:
    lines = [
        "# WebUntis endpoint probe",
        "",
        f"Run `{run_dir.name}`, range {args.start} .. {args.end}.",
        "",
        "Answers open questions 1-4 and 6 of section 10 of the design document.",
        "`?` marks a speculative endpoint - a 404 there is an expected outcome, not a bug.",
        "",
    ]

    for summary in summaries:
        lines += [f"## {summary['profile']}", ""]
        if summary.get("login") != "ok":
            lines += [f"**Login failed:** {summary.get('login')}", ""]
            continue
        students = ", ".join(f"{s['name']} (`{s['id']}`)" for s in summary.get("students", []))
        lines += [
            f"- roles `{summary.get('roles')}`, parent person `{summary.get('parent_person_id')}`",
            f"- children visible to this login: {students or 'none'}",
            f"- probed as studentId `{summary.get('student_id_used')}`, "
            f"klasseId `{summary.get('klasse_id', 'not discovered')}`",
            f"- tenant-id: `{summary.get('tenant_id') or 'NOT FOUND'}` (open question 4)",
            "",
            "| ? | Probe | Auth | HTTP | Verdict |",
            "|---|-------|------|------|---------|",
        ]
        for record in summary.get("probes", []):
            flag = "?" if record["confidence"] == "speculative" else ""
            items = f" ({record['items']} items)" if record.get("items") else ""
            lines.append(
                f"| {flag} | `{record['name']}` | {record['auth']} | "
                f"{record.get('http_status', '-')} | {record['verdict']}{items} |"
            )
        lines.append("")

        trails = [r for r in summary.get("probes", []) if r.get("discovery_trail")]
        if trails:
            lines += ["Parameters discovered from the server's own error messages:", ""]
            for record in trails:
                lines.append(f"- `{record['name']}`: {', '.join(record['discovery_trail']) or '-'}")
            lines.append("")

    # Capability differences between tenants are open question 3, so make them explicit.
    good = [s for s in summaries if s.get("login") == "ok"]
    if len(good) > 1:
        lines += ["## Capability differences (open question 3)", ""]
        for summary in good:
            others = set().union(
                *[set(other.get("permissions", [])) for other in good if other is not summary]
            )
            only = sorted(set(summary.get("permissions", [])) - others)
            lines.append(f"- **{summary['profile']}** only: {', '.join(only) if only else 'none'}")
        lines.append("")
        for summary in good:
            others = set().union(
                *[set(other.get("views", [])) for other in good if other is not summary]
            )
            only = sorted(set(summary.get("views", [])) - others)
            lines.append(
                f"- **{summary['profile']}** views only: {', '.join(only) if only else 'none'}"
            )
        lines.append("")

    lines += [
        "## What to do with this",
        "",
        "1. Any endpoint that behaves differently between profiles is open question 3.",
        "2. Read the `shape` entries in `_summary.json` before fixing the domain model.",
        "3. Anonymise the dumped payloads before committing them as test fixtures.",
        "",
    ]

    report = run_dir / "report.md"
    report.write_text("\n".join(lines), encoding="utf-8")
    return report


# ------------------------------------------------------------------------------------ cli


def parse_args(argv: list[str]) -> argparse.Namespace:
    today = date.today()
    default_config = Path(
        os.environ.get("UNTIS_MCP_CONFIG", str(REPO_ROOT / "config" / "profiles.toml"))
    )
    parser = argparse.ArgumentParser(
        description="Probe the WebUntis endpoints of section 5.2 against the real tenants."
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=default_config,
        help="profile configuration (default: config/profiles.toml or $UNTIS_MCP_CONFIG)",
    )
    parser.add_argument("--profile", action="append", help="probe only this profile (repeatable)")
    parser.add_argument(
        "--only",
        action="append",
        help="run only these probes by name (repeatable), e.g. --only calendar-entry-detail-v2",
    )
    parser.add_argument(
        "--student-id",
        type=int,
        help="probe this child instead of the first one app/data reports",
    )
    parser.add_argument(
        "--start",
        type=date.fromisoformat,
        default=today - timedelta(days=14),
        help="range start, ISO date (default: 14 days ago)",
    )
    parser.add_argument(
        "--end",
        type=date.fromisoformat,
        default=today + timedelta(days=21),
        help="range end, ISO date (default: in 21 days)",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=REPO_ROOT / "probe-output",
        help="output directory (default: probe-output/)",
    )
    parser.add_argument("--timeout", type=float, default=30.0, help="HTTP timeout in seconds")
    return parser.parse_args(argv)


async def main_async(argv: list[str]) -> int:
    args = parse_args(argv)
    profiles = load_profiles(args.config)

    if args.profile:
        wanted = set(args.profile)
        unknown = wanted - {profile.name for profile in profiles}
        if unknown:
            sys.exit(
                f"unknown profile(s): {', '.join(sorted(unknown))}; "
                f"configured: {', '.join(profile.name for profile in profiles)}"
            )
        profiles = [profile for profile in profiles if profile.name in wanted]

    run_dir = args.out / datetime.now().strftime("%Y%m%d-%H%M%S")
    run_dir.mkdir(parents=True, exist_ok=True)

    print(f"probing {len(profiles)} profile(s), {args.start} .. {args.end}")
    summaries = [await probe_profile(profile, args, run_dir) for profile in profiles]

    report = write_report(summaries, run_dir, args)
    print(f"\nreport:   {report}")
    print(f"payloads: {run_dir}")
    print("These files contain real personal data - anonymise before using them as fixtures.")

    return 0 if all(summary.get("login") == "ok" for summary in summaries) else 1


def main() -> int:
    return asyncio.run(main_async(sys.argv[1:]))


if __name__ == "__main__":
    raise SystemExit(main())
