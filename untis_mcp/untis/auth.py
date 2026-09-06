"""The login flow and account discovery of section 5.1.

Four requests turn a username and password into everything the data endpoints need:

1. JSON-RPC ``authenticate`` — a session id, set as the ``JSESSIONID`` cookie.
2. the ``schoolname`` cookie, base64 with a leading underscore.
3. ``/api/token/new`` — a JWT, returned as a plain string rather than JSON.
4. ``/api/rest/view/v1/app/data`` — the tenant id, the children, the permissions.

Step 4 is the load-bearing one: the ``personId`` from step 1 is the *parent*, and every
element-scoped endpoint rejects it with HTTP 403.
"""

from __future__ import annotations

import base64
import logging
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

from untis_mcp.config import ProfileConfig
from untis_mcp.errors import StudentAmbiguous, UntisAuthError, UntisUnavailable
from untis_mcp.untis.capabilities import Capabilities

logger = logging.getLogger(__name__)

CLIENT_ID = "untis-mcp"

# WebUntis expires a session after roughly 10 minutes of inactivity; refresh earlier so a
# request does not have to fail first (ADR-011).
SESSION_TTL_SECONDS = 8 * 60

# JSON-RPC codes that mean "your session is gone", as opposed to "you may not do that".
RPC_NOT_AUTHENTICATED = {-8520, -8521}


@dataclass
class Session:
    """One authenticated WebUntis session for one profile."""

    session_id: str
    jwt: str | None
    tenant_id: str | None
    student_id: int
    student_name: str
    parent_person_id: int | None
    capabilities: Capabilities = field(default_factory=Capabilities)
    created_at: float = field(default_factory=time.monotonic)

    @property
    def age_seconds(self) -> float:
        return time.monotonic() - self.created_at

    @property
    def is_stale(self) -> bool:
        return self.age_seconds >= SESSION_TTL_SECONDS


class JsonRpcError(Exception):
    """A JSON-RPC error envelope, kept structured so the caller can classify it."""

    def __init__(self, code: int | None, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(f"JSON-RPC error {code}: {message}")

    @property
    def means_session_expired(self) -> bool:
        return self.code in RPC_NOT_AUTHENTICATED


async def json_rpc(
    http: httpx.AsyncClient,
    profile: ProfileConfig,
    method: str,
    params: Any = None,
) -> Any:
    """One JSON-RPC call. Raises `JsonRpcError` for an error envelope."""
    try:
        response = await http.post(
            f"{profile.server}/WebUntis/jsonrpc.do",
            params={"school": profile.school},
            json={
                "id": CLIENT_ID,
                "method": method,
                "params": params if params is not None else {},
                "jsonrpc": "2.0",
            },
        )
        response.raise_for_status()
        payload = response.json()
    except httpx.HTTPError as exc:
        raise UntisUnavailable(profile.name, f"{method} failed: {type(exc).__name__}") from exc
    except ValueError as exc:
        raise UntisUnavailable(profile.name, f"{method} returned no JSON") from exc

    if isinstance(payload, dict) and payload.get("error"):
        error = payload["error"]
        raise JsonRpcError(error.get("code"), str(error.get("message", "")))
    return payload.get("result") if isinstance(payload, dict) else payload


def apply_cookies(http: httpx.AsyncClient, profile: ProfileConfig, session_id: str) -> None:
    """Set the two cookies every later request needs (section 5.1, step 2)."""
    host = httpx.URL(profile.server).host
    encoded_school = "_" + base64.b64encode(profile.school.encode()).decode()
    http.cookies.set("JSESSIONID", session_id, domain=host, path="/")
    http.cookies.set("schoolname", encoded_school, domain=host, path="/")


async def login(http: httpx.AsyncClient, profile: ProfileConfig) -> Session:
    """Authenticate and discover the account. Never logs the password or the tokens."""
    try:
        result = await json_rpc(
            http,
            profile,
            "authenticate",
            {"user": profile.username, "password": profile.secret, "client": CLIENT_ID},
        )
    except JsonRpcError as exc:
        raise UntisAuthError(profile.name, f"login rejected: {exc.message}") from exc

    if not isinstance(result, dict) or not result.get("sessionId"):
        raise UntisAuthError(profile.name, "login returned no session id")

    session_id = str(result["sessionId"])
    apply_cookies(http, profile, session_id)

    jwt = await _fetch_token(http, profile)
    app_data = await _fetch_app_data(http, profile, jwt) if jwt else None

    tenant_id = _tenant_id(app_data)
    students = _students(app_data)
    student_id, student_name = _choose_student(profile, students, result.get("personId"))

    session = Session(
        session_id=session_id,
        jwt=jwt,
        tenant_id=tenant_id,
        student_id=student_id,
        student_name=student_name,
        parent_person_id=result.get("personId"),
        capabilities=Capabilities.from_app_data(app_data),
    )
    logger.info(
        "profile %r: logged in as %s, reading student %s (id %s), tenant %s",
        profile.name,
        profile.username,
        student_name or "?",
        student_id,
        tenant_id or "unknown",
    )
    return session


async def logout(http: httpx.AsyncClient, profile: ProfileConfig) -> None:
    """Best-effort logout; a failure here must never surface to the caller."""
    try:
        await json_rpc(http, profile, "logout")
    except (JsonRpcError, UntisUnavailable):
        logger.debug("profile %r: logout failed, ignoring", profile.name)


# ------------------------------------------------------------------------ discovery steps


async def _fetch_token(http: httpx.AsyncClient, profile: ProfileConfig) -> str | None:
    """Step 3. The JWT is a bare string; anything else means the tenant said no."""
    try:
        response = await http.get(f"{profile.server}/WebUntis/api/token/new")
    except httpx.HTTPError as exc:
        raise UntisUnavailable(profile.name, f"token request failed: {type(exc).__name__}") from exc

    if response.status_code != 200 or not response.text.strip():
        logger.warning(
            "profile %r: no bearer token (HTTP %s); endpoints needing one will be unavailable",
            profile.name,
            response.status_code,
        )
        return None
    return response.text.strip()


async def _fetch_app_data(
    http: httpx.AsyncClient, profile: ProfileConfig, jwt: str
) -> dict[str, Any] | None:
    """Step 4. Without it there is no tenant id, no student id and no capabilities."""
    try:
        response = await http.get(
            f"{profile.server}/WebUntis/api/rest/view/v1/app/data",
            headers={"Authorization": f"Bearer {jwt}"},
        )
    except httpx.HTTPError as exc:
        raise UntisUnavailable(profile.name, f"app/data failed: {type(exc).__name__}") from exc

    if response.status_code != 200:
        logger.warning("profile %r: app/data answered HTTP %s", profile.name, response.status_code)
        return None
    try:
        payload = response.json()
    except ValueError:
        logger.warning("profile %r: app/data returned no JSON", profile.name)
        return None
    return payload if isinstance(payload, dict) else None


def _tenant_id(app_data: dict[str, Any] | None) -> str | None:
    """`tenant.id` — there is no `tenantId` key anywhere in the payload."""
    tenant = (app_data or {}).get("tenant")
    if isinstance(tenant, dict) and tenant.get("id") is not None:
        return str(tenant["id"])
    return None


def _students(app_data: dict[str, Any] | None) -> list[tuple[int, str]]:
    user = (app_data or {}).get("user")
    raw = user.get("students") if isinstance(user, dict) else None
    students: list[tuple[int, str]] = []
    for item in raw or []:
        if isinstance(item, dict) and isinstance(item.get("id"), int):
            students.append((item["id"], str(item.get("displayName") or "")))
    return students


def _choose_student(
    profile: ProfileConfig,
    students: list[tuple[int, str]],
    parent_person_id: Any,
) -> tuple[int, str]:
    """Pick the child this profile is about (FR-11)."""
    by_id = dict(students)

    if profile.student_id is not None:
        if students and profile.student_id not in by_id:
            raise StudentAmbiguous(profile.name, students)
        return profile.student_id, by_id.get(profile.student_id, "")

    if len(students) == 1:
        return students[0]
    if len(students) > 1:
        raise StudentAmbiguous(profile.name, students)

    # No students listed at all: the account may itself be a pupil, in which case the login's
    # own person id is the right element. Anything else fails later with a clear 403.
    if isinstance(parent_person_id, int) and parent_person_id:
        logger.warning(
            "profile %r: app/data lists no children; falling back to the login's own person id",
            profile.name,
        )
        return parent_person_id, ""
    raise UntisAuthError(profile.name, "could not determine which student to read")
