"""Offline tests for the protocol spike.

The spike talks to two real school tenants with real credentials, so its mechanics are
verified here against a simulated WebUntis before it is pointed at a school server. The
fake mirrors what the tenants actually returned: a parent login whose ``personId`` is the
parent, the children behind ``app/data``, and endpoints that answer a malformed request by
naming the parameter they wanted.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from datetime import date
from pathlib import Path

import httpx
import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent

_spec = importlib.util.spec_from_file_location(
    "probe_endpoints", REPO_ROOT / "scripts" / "probe_endpoints.py"
)
assert _spec and _spec.loader
probe = importlib.util.module_from_spec(_spec)
# Register before executing: @dataclass resolves annotations via sys.modules.
sys.modules["probe_endpoints"] = probe
_spec.loader.exec_module(probe)


SERVER = "https://ajax.webuntis.com"
PROFILE = probe.Profile(
    name="anna",
    description="Anna, Gymnasium Musterstadt",
    server=SERVER,
    school="gym-musterstadt",
    username="anna.mustermann",
    password="secret",
)
PARENT_PERSON_ID = 4242
STUDENT_ID = 1001
START = date(2026, 8, 24)
END = date(2026, 9, 27)


# ------------------------------------------------------------------------- fake WebUntis


def _missing(name: str, java_type: str) -> httpx.Response:
    """The 500 WebUntis answers with when a query parameter is missing."""
    return httpx.Response(
        500,
        json={
            "errorCode": "INTERNAL_ERROR",
            "errorMessage": (
                f"Required request parameter '{name}' for method parameter type "
                f"{java_type} is not present"
            ),
        },
    )


def _bad_value(value: str, java_type: str) -> httpx.Response:
    """The 400 WebUntis answers with when a value does not fit the expected Java type."""
    return httpx.Response(
        400,
        json={
            "errorCode": "BAD_REQUEST",
            "errorMessage": (
                f"The parameter value '{value}' is no valid value for the expected type: "
                f"'class {java_type}'"
            ),
        },
    )


def fake_webuntis(request: httpx.Request) -> httpx.Response:
    """A WebUntis stand-in covering the parts of sections 5.1 and 5.2 under test."""
    path = request.url.path
    params = request.url.params

    if path == "/WebUntis/jsonrpc.do":
        body = json.loads(request.content)
        if body["method"] == "authenticate":
            return httpx.Response(
                200,
                json={
                    "jsonrpc": "2.0",
                    "id": body["id"],
                    "result": {
                        "sessionId": "SESSION-ABC",
                        # 12 = LEGAL_GUARDIAN: the parent, not the child.
                        "personType": 12,
                        "personId": PARENT_PERSON_ID,
                        "klasseId": 0,
                    },
                },
            )
        if body["method"] == "getTimetable":
            if body["params"].get("id") != STUDENT_ID:
                return httpx.Response(
                    200,
                    json={
                        "jsonrpc": "2.0",
                        "id": body["id"],
                        "error": {"code": -7001, "message": "invalid elementType: 12"},
                    },
                )
            return httpx.Response(
                200,
                json={
                    "jsonrpc": "2.0",
                    "id": body["id"],
                    "result": [{"id": 1, "date": 20260907, "startTime": 830, "code": "cancelled"}],
                },
            )
        return httpx.Response(
            200,
            json={
                "jsonrpc": "2.0",
                "id": body["id"],
                "error": {"code": -8509, "message": "no right for getTimegridUnits()"},
            },
        )

    if path == "/WebUntis/api/token/new":
        return httpx.Response(200, text="JWT-TOKEN-XYZ", headers={"content-type": "text/plain"})

    if path == "/WebUntis/api/rest/view/v1/app/data":
        if request.headers.get("Authorization") != "Bearer JWT-TOKEN-XYZ":
            return httpx.Response(401, json={"error": "unauthorized"})
        return httpx.Response(
            200,
            json={
                # The tenant id lives here, not under a "tenantId" key anywhere.
                "tenant": {"displayName": "Gymnasium Musterstadt", "id": "9900001"},
                "user": {
                    "name": "anna.mustermann",
                    "roles": ["LEGAL_GUARDIAN"],
                    "person": {"displayName": "anna.mustermann", "id": PARENT_PERSON_ID},
                    "students": [{"displayName": "Mustermann", "id": STUDENT_ID}],
                    "permissions": {"views": ["TODAY", "MESSAGE_CENTER"]},
                },
                "permissions": ["MESSAGES:R:0:ALL", "TT_VIEW:R:5:ASSIGNED"],
            },
        )

    if path == "/WebUntis/api/public/timetable/weekly/data":
        if params.get("elementId") != str(STUDENT_ID):
            return httpx.Response(403, json={"errorCode": "FORBIDDEN", "errorMessage": "no right"})
        return httpx.Response(
            200,
            json={
                "data": {
                    "result": {
                        "data": {
                            "elements": [
                                {"type": 5, "id": STUDENT_ID, "name": "Mustermann"},
                                {"type": 1, "id": 331, "name": "7c"},
                            ],
                            "elementPeriods": {
                                str(STUDENT_ID): [{"id": 9, "cellState": "STANDARD"}]
                            },
                        }
                    }
                }
            },
        )

    if path == "/WebUntis/api/rest/view/v1/messages":
        if request.headers.get("tenant-id") != "9900001":
            return httpx.Response(403, json={"errorMessage": "missing tenant-id"})
        return httpx.Response(200, json={"incomingMessages": [{"id": 1, "subject": "Ausfall"}]})

    if path == "/WebUntis/api/classreg/absences/students":
        return httpx.Response(200, json={"data": {"absences": []}})

    if path == "/WebUntis/api/rest/view/v2/calendar-entry/detail":
        # Answers one missing parameter at a time - the oracle discover_parameters follows.
        for name, java_type in (
            ("elementId", "Long"),
            ("elementType", "Long"),
            ("startDate", "Integer"),
        ):
            if name not in params:
                return _missing(name, java_type)
        # ... and rejects a wrongly typed value by naming the value, not the parameter.
        if not params["elementType"].isdigit():
            return _bad_value(params["elementType"], "java.lang.Long")
        if not params["startDate"].isdigit():
            return _bad_value(params["startDate"], "java.lang.Integer")
        return httpx.Response(200, json={"calendarEntries": [{"id": 5, "type": "EXAM"}]})

    return httpx.Response(200, json={"data": {"result": {"ok": True}}})


def make_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(fake_webuntis))


# ---------------------------------------------------------------------- authentication


async def test_authenticate_reads_the_child_and_the_tenant_out_of_app_data():
    async with make_client() as client:
        ctx = await probe.authenticate(client, PROFILE)

    assert ctx.jwt == "JWT-TOKEN-XYZ"
    # The parent is who logged in; the child is who the endpoints want.
    assert ctx.person_id == PARENT_PERSON_ID
    assert ctx.roles == ["LEGAL_GUARDIAN"]
    assert [student["id"] for student in ctx.students] == [STUDENT_ID]
    # tenant.id, not tenantId - the first run against the real tenants missed this.
    assert ctx.tenant_id == "9900001"
    assert "MESSAGES:R:0:ALL" in ctx.permissions
    assert "MESSAGE_CENTER" in ctx.views


async def test_schoolname_cookie_is_base64_with_leading_underscore():
    async with make_client() as client:
        await probe.authenticate(client, PROFILE)
        cookies = dict(client.cookies)

    assert cookies["JSESSIONID"] == "SESSION-ABC"
    # base64("gym-musterstadt"), prefixed with "_"
    assert cookies["schoolname"] == "_Z3ltLW11c3RlcnN0YWR0"


# ------------------------------------------------------------------------------- probes


async def test_probe_records_success_and_dumps_the_payload(tmp_path: Path):
    async with make_client() as client:
        ctx = await probe.authenticate(client, PROFILE)
        record, payload = await probe.run_probe(
            client,
            PROFILE,
            ctx,
            probe.Probe(
                "messages-inbox",
                "rest",
                "/WebUntis/api/rest/view/v1/messages",
                auth="jwt+tenant",
            ),
            tmp_path,
        )

    assert record["verdict"] == "ok"
    assert record["items"] == 1
    assert payload["incomingMessages"][0]["subject"] == "Ausfall"
    dumped = json.loads((tmp_path / "messages-inbox.json").read_text(encoding="utf-8"))
    assert dumped["incomingMessages"][0]["subject"] == "Ausfall"


async def test_the_parents_person_id_is_rejected_where_the_childs_is_accepted(tmp_path: Path):
    """The failure mode of the first real run, pinned so it cannot come back."""
    async with make_client() as client:
        ctx = await probe.authenticate(client, PROFILE)

        as_parent = probe.build_probes(ctx, PARENT_PERSON_ID, None, START, END)[0]
        as_child = probe.build_probes(ctx, STUDENT_ID, None, START, END)[0]

        parent_record, _ = await probe.run_probe(client, PROFILE, ctx, as_parent, tmp_path)
        child_record, child_payload = await probe.run_probe(
            client, PROFILE, ctx, as_child, tmp_path
        )

    assert parent_record["verdict"] == "http 403"
    assert child_record["verdict"] == "ok"
    assert child_payload is not None


async def test_probe_reports_a_denied_json_rpc_method(tmp_path: Path):
    async with make_client() as client:
        ctx = await probe.authenticate(client, PROFILE)
        record, payload = await probe.run_probe(
            client, PROFILE, ctx, probe.Probe("timegrid-units", "rpc", "getTimegridUnits"), tmp_path
        )

    assert record["verdict"].startswith("denied")
    assert "-8509" in record["verdict"]
    assert payload is None


async def test_an_empty_result_is_distinguished_from_a_populated_one(tmp_path: Path):
    async with make_client() as client:
        ctx = await probe.authenticate(client, PROFILE)
        record, _ = await probe.run_probe(
            client,
            PROFILE,
            ctx,
            probe.Probe("absences", "rest", "/WebUntis/api/classreg/absences/students"),
            tmp_path,
        )

    # An empty range must not read as "endpoint unavailable" - that would answer
    # open question 3 wrongly.
    assert record["verdict"] == "ok (empty)"
    assert record["http_status"] == 200


# ------------------------------------------------------------------ parameter discovery


async def test_discovery_follows_the_servers_complaints_until_the_call_succeeds(tmp_path: Path):
    async with make_client() as client:
        ctx = await probe.authenticate(client, PROFILE)
        record, payload = await probe.run_probe(
            client,
            PROFILE,
            ctx,
            probe.Probe(
                "calendar-entry-detail-v2",
                "rest",
                "/WebUntis/api/rest/view/v2/calendar-entry/detail",
                auth="jwt+tenant",
                confidence="speculative",
                discover=True,
            ),
            tmp_path,
            student_id=STUDENT_ID,
            start=START,
            end=END,
        )

    assert record["verdict"] == "ok"
    assert payload["calendarEntries"][0]["type"] == "EXAM"
    # Each round is recorded, because the parameter set is the actual finding here.
    assert record["discovery_trail"] == [
        f"elementId={STUDENT_ID!r}",
        "elementType=5",
        f"startDate={START.isoformat()!r}",
        # Both date candidates are rejected in turn until the compact form fits.
        f"startDate={START.isoformat() + 'T00:00'!r}",
        f"startDate={START.strftime('%Y%m%d')!r}",
    ]
    assert record["params"]["elementType"] == 5
    assert record["params"]["startDate"] == START.strftime("%Y%m%d")


async def test_discovery_gives_up_instead_of_looping_on_an_unguessable_parameter(tmp_path: Path):
    def stubborn(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/WebUntis/jsonrpc.do":
            return fake_webuntis(request)
        return _missing("someUnknownThing", "SomeEnum")

    async with httpx.AsyncClient(transport=httpx.MockTransport(stubborn)) as client:
        ctx = probe.AuthContext(session_id="s", person_type=12, person_id=1, klasse_id=0)
        record, payload = await probe.run_probe(
            client,
            PROFILE,
            ctx,
            probe.Probe("guess", "rest", "/WebUntis/api/rest/view/v2/x", discover=True),
            tmp_path,
            student_id=STUDENT_ID,
            start=START,
            end=END,
        )

    assert record["verdict"] == "http 500"
    assert record["discovery_trail"] == ["someUnknownThing: out of candidates"]
    assert payload is None


# ------------------------------------------------------------------------- data plumbing


def test_klasse_id_is_recovered_from_the_timetable_payload():
    """authenticate reports klasseId 0 for a parent, but /api/exams demands a real one."""
    payload = {
        "data": {
            "result": {
                "data": {
                    "elements": [
                        {"type": 5, "id": STUDENT_ID, "name": "Mustermann"},
                        {"type": 1, "id": 331, "name": "7c"},
                    ]
                }
            }
        }
    }

    assert probe.find_klasse_id(payload) == 331
    assert probe.find_klasse_id({"data": {"result": {}}}) is None


def test_probe_set_covers_the_endpoint_map():
    ctx = probe.AuthContext(session_id="s", person_type=12, person_id=PARENT_PERSON_ID, klasse_id=0)

    probes = probe.build_probes(ctx, STUDENT_ID, 331, date(2026, 9, 7), date(2026, 9, 30))
    by_name = {item.name: item for item in probes}

    # The two date formats of section 5.3 must both appear, in the right places.
    assert by_name["timetable-weekly-data"].params["date"] == "2026-09-07"
    assert by_name["absences"].params["startDate"] == "20260907"
    # Every element-scoped probe addresses the child, never the parent.
    assert by_name["timetable-weekly-data"].params["elementId"] == STUDENT_ID
    assert by_name["timetable-jsonrpc"].params["id"] == STUDENT_ID
    assert by_name["timetable-jsonrpc"].params["type"] == 5  # not personType 12
    assert by_name["absences"].params["studentId"] == STUDENT_ID
    assert by_name["exams"].params["klasseId"] == 331
    # weekly/data wants the Monday of the week, not the raw start date.
    monday = probe.build_probes(ctx, STUDENT_ID, None, date(2026, 9, 10), date(2026, 9, 30))
    assert {p.name: p for p in monday}["timetable-weekly-data"].params["date"] == "2026-09-07"
    # The endpoints the whole spike exists for run in discovery mode.
    assert by_name["calendar-entry-detail-v2"].discover


# ------------------------------------------------------------- configuration and redaction


def test_load_profiles_resolves_password_env(tmp_path: Path, monkeypatch):
    config = tmp_path / "profiles.toml"
    config.write_text(
        """
[[profiles]]
name = "ben"
description = "Ben"
server = "https://herakles.webuntis.com/"
school = "rs-beispieldorf"
username = "ben.mustermann"
password_env = "UNTIS_BEN_PASSWORD"
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("UNTIS_BEN_PASSWORD", "from-env")

    (profile,) = probe.load_profiles(config)

    assert profile.password == "from-env"
    assert profile.server == "https://herakles.webuntis.com"  # trailing slash stripped


def test_load_profiles_fails_loudly_on_an_empty_password_env(tmp_path: Path, monkeypatch):
    config = tmp_path / "profiles.toml"
    config.write_text(
        """
[[profiles]]
name = "ben"
server = "https://herakles.webuntis.com"
school = "rs-beispieldorf"
username = "ben.mustermann"
password_env = "UNTIS_MISSING"
""",
        encoding="utf-8",
    )
    monkeypatch.delenv("UNTIS_MISSING", raising=False)

    with pytest.raises(SystemExit) as excinfo:
        probe.load_profiles(config)
    assert "UNTIS_MISSING" in str(excinfo.value)


def test_redact_removes_secrets_at_any_depth():
    payload = {
        "sessionId": "SESSION-ABC",
        "user": {"name": "Anna", "auth": {"token": "JWT", "password": "hunter2"}},
        "list": [{"jwt": "x"}],
    }

    cleaned = probe.redact(payload)

    assert cleaned["sessionId"] == "<redacted>"
    assert cleaned["user"]["auth"] == {"token": "<redacted>", "password": "<redacted>"}
    assert cleaned["list"][0]["jwt"] == "<redacted>"
    assert cleaned["user"]["name"] == "Anna"  # payload data itself survives


def test_emptiness_is_detected_through_any_envelope():
    # "endpoint works, nothing in this range" ...
    assert probe.payload_is_empty({"data": {"absences": []}})
    assert probe.payload_is_empty([])
    assert probe.payload_is_empty({"incomingMessages": []})
    # ... versus "endpoint works and returned records".
    assert not probe.payload_is_empty({"incomingMessages": [{"id": 1}]})
    assert not probe.payload_is_empty([{"id": 1}])
    assert not probe.payload_is_empty({"data": {"result": {"currentSchoolyear": 42}}})
