"""The client layer: login, discovery, session lifetime and capability gating."""

from __future__ import annotations

import asyncio

import httpx
import pytest

from tests.fake_untis import (
    PARENT_PERSON_ID,
    STUDENT_ID,
    TENANT_ID,
    TENANT_WITHOUT_MESSAGES,
    VIEWS_WITHOUT_MESSAGES,
    FakeUntis,
)
from untis_mcp.config import ProfileConfig
from untis_mcp.errors import (
    FeatureNotAvailable,
    StudentAmbiguous,
    UntisAuthError,
    UntisError,
    UntisUnavailable,
)
from untis_mcp.untis import client as client_module
from untis_mcp.untis.auth import SESSION_TTL_SECONDS
from untis_mcp.untis.client import LOGIN_BLOCK_SECONDS, ClientRegistry, UntisClient

TIMETABLE = "/WebUntis/api/rest/view/v1/timetable/entries"


def profile(**overrides) -> ProfileConfig:
    data = {
        "name": "anna",
        "description": "Anna, Gymnasium Musterstadt",
        "server": "https://ajax.webuntis.com",
        "school": "gym-musterstadt",
        "username": "anna.mustermann",
        "password": "anna-plaintext",
    }
    data.update(overrides)
    return ProfileConfig.model_validate(data)


def make_client(fake: FakeUntis, **profile_overrides) -> UntisClient:
    return UntisClient(profile(**profile_overrides), http=fake.http_client())


# ---------------------------------------------------------------------- login and discovery


async def test_login_reads_the_child_the_tenant_and_the_permissions():
    fake = FakeUntis()
    client = make_client(fake)

    session = await client.session()

    # The login's own personId is the parent; the child comes from app/data.
    assert session.parent_person_id == PARENT_PERSON_ID
    assert session.student_id == STUDENT_ID
    assert session.student_name == "Mustermann"
    assert session.tenant_id == TENANT_ID
    assert session.jwt == "JWT-TOKEN"
    assert "MESSAGES" in session.capabilities.permissions


async def test_wrong_credentials_name_the_profile():
    client = make_client(FakeUntis(valid_credentials=False))

    with pytest.raises(UntisAuthError) as excinfo:
        await client.session()

    assert "'anna'" in str(excinfo.value)
    assert "bad credentials" in str(excinfo.value)


async def test_an_unreachable_school_is_reported_as_unavailable():
    client = make_client(FakeUntis(unreachable=True))

    with pytest.raises(UntisUnavailable, match="'anna'"):
        await client.session()


# --------------------------------------------------------------- blocking after a rejection


async def test_a_rejected_login_is_not_repeated_within_the_block():
    """ADR-013: later calls fail at once instead of spending the school's lockout budget."""
    fake = FakeUntis(valid_credentials=False)
    client = make_client(fake)

    with pytest.raises(UntisAuthError):
        await client.session()
    for _ in range(5):
        with pytest.raises(UntisAuthError) as excinfo:
            await client.get(TIMETABLE)

    assert fake.rejected_logins == 1
    assert fake.data_requests == []
    message = str(excinfo.value)
    assert "'anna'" in message
    assert "bad credentials" in message
    assert "15 minute(s)" in message
    assert "do not retry" in message


async def test_the_login_is_attempted_again_once_the_block_has_passed(monkeypatch):
    fake = FakeUntis(valid_credentials=False)
    client = make_client(fake)
    now = [1000.0]
    monkeypatch.setattr(client_module.time, "monotonic", lambda: now[0])

    with pytest.raises(UntisAuthError):
        await client.session()

    now[0] += LOGIN_BLOCK_SECONDS - 1
    with pytest.raises(UntisAuthError, match="1 minute"):
        await client.session()
    assert fake.rejected_logins == 1

    # The password has been fixed in the meantime.
    fake.valid_credentials = True
    now[0] += 2
    assert (await client.session()).student_id == STUDENT_ID
    assert fake.login_count == 1


async def test_an_unreachable_school_does_not_block_the_login():
    """Only a rejection counts against the account; an outage is retried on the next call."""
    fake = FakeUntis(unreachable=True)
    client = make_client(fake)

    with pytest.raises(UntisUnavailable):
        await client.session()
    fake.unreachable = False

    assert (await client.session()).student_id == STUDENT_ID


async def test_a_blocked_profile_does_not_block_the_other():
    rejected = make_client(FakeUntis(valid_credentials=False), name="ben")
    healthy = make_client(FakeUntis())

    with pytest.raises(UntisAuthError):
        await rejected.session()

    assert (await healthy.session()).student_id == STUDENT_ID


async def test_a_tenant_that_issues_no_token_still_logs_in():
    """Cookie-only endpoints must keep working when the JWT is refused."""
    fake = FakeUntis(issue_token=False)
    client = make_client(fake)

    session = await client.session()

    assert session.jwt is None
    assert session.tenant_id is None
    # ... but an endpoint that needs the token fails with an explanation, not an AttributeError.
    with pytest.raises(UntisUnavailable, match="bearer token"):
        await client.get(TIMETABLE, needs_token=True)


# ------------------------------------------------------------------- choosing the student


async def test_a_single_child_is_selected_automatically():
    client = make_client(FakeUntis())
    assert (await client.session()).student_id == STUDENT_ID


async def test_several_children_without_a_configured_id_is_an_error_listing_them():
    fake = FakeUntis(
        students=[
            {"id": 1001, "displayName": "Mustermann A"},
            {"id": 1002, "displayName": "Mustermann B"},
        ]
    )
    client = make_client(fake)

    with pytest.raises(StudentAmbiguous) as excinfo:
        await client.session()

    message = str(excinfo.value)
    assert "1001" in message and "1002" in message
    assert "student_id" in message


async def test_a_configured_student_id_picks_one_of_several_children():
    fake = FakeUntis(
        students=[
            {"id": 1001, "displayName": "Mustermann A"},
            {"id": 1002, "displayName": "Mustermann B"},
        ]
    )
    client = make_client(fake, student_id=1002)

    session = await client.session()

    assert session.student_id == 1002
    assert session.student_name == "Mustermann B"


async def test_a_configured_student_id_the_login_cannot_see_is_rejected():
    client = make_client(FakeUntis(), student_id=9999)

    with pytest.raises(StudentAmbiguous):
        await client.session()


# --------------------------------------------------------------------- session lifetime


async def test_the_session_is_reused_across_requests():
    fake = FakeUntis()
    client = make_client(fake)

    await client.get(TIMETABLE)
    await client.get(TIMETABLE)

    assert fake.login_count == 1


async def test_concurrent_first_requests_produce_exactly_one_login():
    """ADR-005: the per-profile lock is what prevents a login storm."""
    fake = FakeUntis()
    client = make_client(fake)

    await asyncio.gather(*(client.get(TIMETABLE) for _ in range(5)))

    assert fake.login_count == 1
    assert len(fake.data_requests) == 5


async def test_a_stale_session_is_refreshed_before_it_is_used(monkeypatch):
    """ADR-011: the proactive TTL, so the common case costs no failed request."""
    fake = FakeUntis()
    client = make_client(fake)

    session = await client.session()
    # Pretend the session was created just over the TTL ago.
    session.created_at -= SESSION_TTL_SECONDS + 1
    await client.get(TIMETABLE)

    assert fake.login_count == 2
    # The retried request carried the *new* session, not the expired one.
    assert fake.session_ids[-1] == "SESSION-2"


async def test_a_rejected_session_is_retried_once(monkeypatch):
    """ADR-011: the reactive safety net, for a session that dies early."""
    fake = FakeUntis(expire_next_request=True)
    client = make_client(fake)

    payload = await client.get(TIMETABLE)

    assert payload["ok"] is True
    assert fake.login_count == 2
    assert fake.data_requests == [TIMETABLE, TIMETABLE]


async def test_a_forbidden_endpoint_is_not_retried():
    """403 is an answer, not an outage — retrying it doubles the cost and still fails."""
    fake = FakeUntis(forbid_paths={TIMETABLE})
    client = make_client(fake)

    with pytest.raises(UntisError) as excinfo:
        await client.get(TIMETABLE)

    assert fake.login_count == 1
    assert fake.data_requests == [TIMETABLE]
    assert "do not retry" in str(excinfo.value)


async def test_json_rpc_retries_once_on_an_expired_session():
    fake = FakeUntis(rpc_session_expired_once=True)
    client = make_client(fake)

    result = await client.rpc("getCurrentSchoolyear")

    assert result["id"] == 27
    assert fake.login_count == 2


async def test_json_rpc_does_not_retry_a_refusal():
    fake = FakeUntis()
    client = make_client(fake)

    with pytest.raises(UntisError, match="no right"):
        await client.rpc("getTimegridUnits")

    assert fake.login_count == 1


# ------------------------------------------------------------------------- request shape


async def test_a_token_endpoint_sends_the_bearer_and_the_tenant_header():
    fake = FakeUntis()
    client = make_client(fake)

    await client.get(TIMETABLE, {"start": "2026-09-07"}, needs_token=True)

    headers = fake.seen_headers[-1]
    assert headers["authorization"] == "Bearer JWT-TOKEN"
    assert headers["tenant-id"] == TENANT_ID
    assert "JSESSIONID=SESSION-1" in headers["cookie"]
    # The school name travels base64-encoded with a leading underscore.
    assert "schoolname=_Z3ltLW11c3RlcnN0YWR0" in headers["cookie"]


async def test_a_cookie_only_endpoint_sends_no_bearer():
    fake = FakeUntis()
    client = make_client(fake)

    await client.get("/WebUntis/api/classreg/absences/students")

    assert "authorization" not in fake.seen_headers[-1]


# -------------------------------------------------------------------------- capabilities


async def test_capability_gating_refuses_a_tool_the_account_may_not_use():
    """The real difference between the two schools (section 5.4)."""
    fake = FakeUntis(permissions=list(TENANT_WITHOUT_MESSAGES), views=list(VIEWS_WITHOUT_MESSAGES))
    client = make_client(fake)
    await client.session()

    client.require("get_timetable_day")  # granted on both tenants
    with pytest.raises(FeatureNotAvailable) as excinfo:
        client.require("get_messages")

    message = str(excinfo.value)
    assert "MESSAGES" in message and "MESSAGE_CENTER" in message
    assert "do not retry" in message


async def test_supported_tools_reflects_the_account():
    with_messages = make_client(FakeUntis())
    without = make_client(
        FakeUntis(permissions=list(TENANT_WITHOUT_MESSAGES), views=list(VIEWS_WITHOUT_MESSAGES))
    )

    granted = (await with_messages.session()).capabilities.supported_tools
    denied = (await without.session()).capabilities.supported_tools

    assert "get_messages" in granted
    assert "get_messages" not in denied
    # Messages of the day are ungated, so both keep them.
    assert "get_messages_of_day" in granted and "get_messages_of_day" in denied
    assert "get_timetable_week" in denied


async def test_capabilities_are_unknown_before_the_first_login():
    client = make_client(FakeUntis())

    assert client.capabilities is None
    # Nothing is refused while nothing is known — the request itself will say.
    client.require("get_messages")


# ------------------------------------------------------------------------------ registry


def test_the_registry_gives_each_profile_its_own_client():
    from untis_mcp.config import Config

    config = Config.model_validate(
        {
            "profiles": [
                profile().model_dump(mode="json") | {"password": "a"},
                profile(name="ben", server="https://herakles.webuntis.com").model_dump(mode="json")
                | {"password": "b"},
            ]
        }
    )
    registry = ClientRegistry(config)

    anna = registry.for_profile(config.get("anna"))
    ben = registry.for_profile(config.get("ben"))

    assert anna is registry.for_profile(config.get("anna"))  # reused
    assert anna is not ben
    # Separate cookie jars: nothing from one school can leak into a request to the other.
    assert anna._http.cookies is not ben._http.cookies


async def test_closing_the_registry_logs_out_and_closes_the_clients():
    fake = FakeUntis()
    from untis_mcp.config import Config

    config = Config.model_validate(
        {"profiles": [profile().model_dump(mode="json") | {"password": "a"}]}
    )
    registry = ClientRegistry(config)
    client = UntisClient(config.get("anna"), http=fake.http_client())
    registry._clients["anna"] = client
    await client.session()

    await registry.aclose()

    assert client.cached_session is None
    with pytest.raises(RuntimeError):
        await client._http.get("https://ajax.webuntis.com/x")


async def test_a_broken_profile_does_not_affect_the_other(monkeypatch):
    """FR-07, at the layer where it is actually decided."""
    healthy = make_client(FakeUntis())
    broken = make_client(FakeUntis(unreachable=True), name="ben")

    with pytest.raises(UntisUnavailable):
        await broken.session()
    assert (await healthy.session()).student_id == STUDENT_ID


async def test_the_http_client_is_only_created_once_per_client():
    fake = FakeUntis()
    client = make_client(fake)
    first = client._http

    await client.session()
    await client.get(TIMETABLE)

    assert client._http is first
    assert isinstance(first, httpx.AsyncClient)
