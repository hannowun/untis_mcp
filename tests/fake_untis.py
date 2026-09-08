"""A configurable stand-in for a WebUntis tenant, shared by the client and tool tests.

It mirrors what the two real tenants did on 2026-09-06 (section 5 of the design document):
the login reports the *parent*, `app/data` carries the tenant id, the children and the
permission lists, and the two schools grant different things. Everything a test needs to
vary — expiring a session, forbidding an endpoint, listing two children — is an attribute.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

# Permission sets observed on the two real tenants, trimmed to what the mapping uses.
TENANT_WITH_MESSAGES = [
    "MESSAGES:R:0:ALL",
    "TT_VIEW:R:5:ASSIGNED",
    "STUDABS_REPORT:DCWR:0:ASSIGNED",
    "HOMEWORK:R:0:ALL",
]
TENANT_WITHOUT_MESSAGES = [
    "TT_VIEW:R:5:ASSIGNED",
    "CLASSSERVICE:R:0:ASSIGNED",
    "HOMEWORK:R:0:ALL",
]
VIEWS_WITH_MESSAGES = [
    "TODAY",
    "TIMETABLE_NEW_STUDENTS_MY",
    "MESSAGE_CENTER",
    "STUDENTABSENCES",
    "STUDENT_HOMEWORK",
]
VIEWS_WITHOUT_MESSAGES = [
    "TODAY",
    "TIMETABLE_NEW_STUDENTS_MY",
    "STUDENTABSENCES",
    "STUDENT_HOMEWORK",
]

PARENT_PERSON_ID = 4242
STUDENT_ID = 1001
TENANT_ID = "9900001"


@dataclass
class FakeUntis:
    """One tenant. Counters and switches let a test drive the paths that matter."""

    # --- what the tenant grants -------------------------------------------------------
    permissions: list[str] = field(default_factory=lambda: list(TENANT_WITH_MESSAGES))
    views: list[str] = field(default_factory=lambda: list(VIEWS_WITH_MESSAGES))
    students: list[dict[str, Any]] = field(
        default_factory=lambda: [{"id": STUDENT_ID, "displayName": "Mustermann"}]
    )
    tenant_id: str | None = TENANT_ID
    issue_token: bool = True

    # --- how it misbehaves ------------------------------------------------------------
    valid_credentials: bool = True
    unreachable: bool = False
    expire_next_request: bool = False
    """Answer the next data request with 401, as an expired session would."""
    forbid_paths: set[str] = field(default_factory=set)
    """Paths that answer 403 — permanently, the way /api/exams does for a parent."""
    rpc_session_expired_once: bool = False

    responses: dict[str, Any] = field(default_factory=dict)
    """Payloads to serve for given paths; anything else gets a trivial 200."""

    # --- what happened ----------------------------------------------------------------
    login_count: int = 0
    data_requests: list[str] = field(default_factory=list)
    data_params: list[dict[str, str]] = field(default_factory=list)
    seen_headers: list[dict[str, str]] = field(default_factory=list)
    session_ids: list[str] = field(default_factory=list)

    def handler(self, request: httpx.Request) -> httpx.Response:
        if self.unreachable:
            raise httpx.ConnectError("connection refused", request=request)

        path = request.url.path
        if path == "/WebUntis/jsonrpc.do":
            return self._json_rpc(request)
        if path == "/WebUntis/api/token/new":
            if not self.issue_token:
                return httpx.Response(403, text="no token for you")
            return httpx.Response(200, text="JWT-TOKEN", headers={"content-type": "text/plain"})
        if path == "/WebUntis/api/rest/view/v1/app/data":
            return self._app_data(request)
        return self._data(request, path)

    # ------------------------------------------------------------------------ endpoints

    def _json_rpc(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        method = body["method"]

        if method == "authenticate":
            if not self.valid_credentials:
                return self._rpc_error(body, -8504, "bad credentials")
            self.login_count += 1
            session_id = f"SESSION-{self.login_count}"
            self.session_ids.append(session_id)
            return httpx.Response(
                200,
                json={
                    "jsonrpc": "2.0",
                    "id": body["id"],
                    "result": {
                        "sessionId": session_id,
                        "personType": 12,  # LEGAL_GUARDIAN
                        "personId": PARENT_PERSON_ID,
                        "klasseId": 0,
                    },
                },
            )

        if method == "logout":
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "result": {}})

        if self.rpc_session_expired_once:
            self.rpc_session_expired_once = False
            return self._rpc_error(body, -8520, "not authenticated")

        if method == "getCurrentSchoolyear":
            return httpx.Response(
                200,
                json={
                    "jsonrpc": "2.0",
                    "id": body["id"],
                    "result": {"id": 27, "startDate": 20260803, "endDate": 20270731},
                },
            )
        return self._rpc_error(body, -8509, f"no right for {method}()")

    def _app_data(self, request: httpx.Request) -> httpx.Response:
        if request.headers.get("Authorization") != "Bearer JWT-TOKEN":
            return httpx.Response(401, json={"errorMessage": "unauthorized"})
        payload: dict[str, Any] = {
            "user": {
                "roles": ["LEGAL_GUARDIAN"],
                "person": {"id": PARENT_PERSON_ID, "displayName": "Mustermann"},
                "students": self.students,
                "permissions": {"views": self.views},
            },
            "permissions": self.permissions,
        }
        if self.tenant_id is not None:
            payload["tenant"] = {"displayName": "Gymnasium Musterstadt", "id": self.tenant_id}
        return httpx.Response(200, json=payload)

    def _data(self, request: httpx.Request, path: str) -> httpx.Response:
        self.data_requests.append(path)
        self.data_params.append(dict(request.url.params))
        self.seen_headers.append(dict(request.headers))

        if path in self.forbid_paths:
            return httpx.Response(403, json={"errorCode": "FORBIDDEN", "errorMessage": "no right"})
        if self.expire_next_request:
            self.expire_next_request = False
            return httpx.Response(401, json={"errorMessage": "session expired"})
        if path in self.responses:
            return httpx.Response(200, json=self.responses[path])
        return httpx.Response(200, json={"ok": True, "path": path})

    @staticmethod
    def _rpc_error(body: dict[str, Any], code: int, message: str) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "jsonrpc": "2.0",
                "id": body["id"],
                "error": {"code": code, "message": message},
            },
        )

    # ---------------------------------------------------------------------------- wiring

    def http_client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self.handler))

    def serve_fixture(self, path: str, name: str) -> FakeUntis:
        """Answer `path` with a payload captured from a real tenant."""
        fixture = Path(__file__).parent / "fixtures" / name
        self.responses[path] = json.loads(fixture.read_text(encoding="utf-8"))
        return self
