"""The per-profile WebUntis client and the registry that owns them.

This is the layer the tools talk to. It hides three things they should not have to know:

* how a session is obtained and refreshed (ADR-005, ADR-011),
* that two tenants are entirely separate — separate HTTP clients, cookie jars and tokens
  (section 5.1, step 7),
* which of cookie, bearer token and `tenant-id` header a given endpoint wants (section 5.2).
"""

from __future__ import annotations

import asyncio
import logging
from datetime import date
from typing import Any

import httpx

from untis_mcp.config import Config, ProfileConfig
from untis_mcp.errors import FeatureNotAvailable, UntisError, UntisUnavailable
from untis_mcp.untis.auth import (
    JsonRpcError,
    Session,
    apply_cookies,
    json_rpc,
    login,
    logout,
)
from untis_mcp.untis.capabilities import Capabilities

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 30.0

TIMETABLE_ENTRIES = "/WebUntis/api/rest/view/v1/timetable/entries"


class UntisClient:
    """Authenticated access to one WebUntis tenant, for one child.

    Sessions are created lazily on first use and shared by every later request, so a broken
    or unreachable school neither delays startup nor affects the other child (FR-07).
    """

    def __init__(
        self,
        profile: ProfileConfig,
        http: httpx.AsyncClient | None = None,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        self.profile = profile
        self._http = http or httpx.AsyncClient(timeout=timeout, follow_redirects=True)
        self._session: Session | None = None
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------ session handling

    @property
    def cached_session(self) -> Session | None:
        """The session as it stands, without logging in. Used by `list_profiles`."""
        return self._session

    @property
    def capabilities(self) -> Capabilities | None:
        return self._session.capabilities if self._session else None

    async def session(self, *, force: bool = False) -> Session:
        """The current session, logging in if there is none or it is stale (ADR-011).

        The lock is what keeps two concurrent tool calls from starting two logins; the
        second waiter re-checks and finds the session the first one created.
        """
        async with self._lock:
            if not force and self._session is not None and not self._session.is_stale:
                return self._session

            if self._session is not None:
                reason = "forced" if force else f"stale after {self._session.age_seconds:.0f}s"
                logger.debug("profile %r: re-authenticating (%s)", self.profile.name, reason)
                self._session = None

            self._session = await login(self._http, self.profile)
            return self._session

    async def aclose(self) -> None:
        if self._session is not None:
            await logout(self._http, self.profile)
            self._session = None
        await self._http.aclose()

    # -------------------------------------------------------------------------- requests

    def require(self, tool: str) -> None:
        """Raise `FeatureNotAvailable` unless this account may use `tool` (ADR-010).

        Only meaningful once a session exists; before that every tool is worth attempting.
        """
        capabilities = self.capabilities
        if capabilities is not None and not capabilities.allows(tool):
            raise FeatureNotAvailable(self.profile.name, tool, capabilities.missing_for(tool))

    async def get(
        self,
        path: str,
        params: dict[str, Any] | None = None,
        *,
        needs_token: bool = False,
    ) -> Any:
        """GET a REST endpoint and return the decoded JSON.

        Retries once after re-authenticating when the server says the session is gone —
        but never on 403, which is a permanent answer rather than an outage (ADR-011).
        """
        session = await self.session()
        response = await self._send(path, params, session, needs_token)

        if response.status_code == 401:
            logger.info("profile %r: session rejected on %s, retrying", self.profile.name, path)
            session = await self.session(force=True)
            response = await self._send(path, params, session, needs_token)

        if response.status_code >= 400:
            raise self._http_error(path, response)

        try:
            return response.json()
        except ValueError as exc:
            raise UntisUnavailable(
                self.profile.name, f"{path} returned no JSON (HTTP {response.status_code})"
            ) from exc

    async def timetable_entries(self, start: date, end: date) -> Any:
        """The primary timetable source of ADR-009, scoped to this profile's child.

        `resourceType` takes the enum *name* here, unlike `elementType` elsewhere, which
        takes the numeric element type (section 5.3).
        """
        session = await self.session()
        return await self.get(
            TIMETABLE_ENTRIES,
            {
                "start": start.isoformat(),
                "end": end.isoformat(),
                "format": 1,
                "resourceType": "STUDENT",
                "resources": session.student_id,
            },
            needs_token=True,
        )

    async def rpc(self, method: str, params: Any = None) -> Any:
        """One JSON-RPC call, with the same single retry on an expired session."""
        await self.session()
        try:
            return await json_rpc(self._http, self.profile, method, params)
        except JsonRpcError as exc:
            if not exc.means_session_expired:
                raise UntisError(self.profile.name, f"{method}: {exc.message}") from exc
            logger.info(
                "profile %r: session expired during %s, retrying", self.profile.name, method
            )

        await self.session(force=True)
        try:
            return await json_rpc(self._http, self.profile, method, params)
        except JsonRpcError as exc:
            raise UntisError(self.profile.name, f"{method}: {exc.message}") from exc

    # --------------------------------------------------------------------------- internals

    async def _send(
        self,
        path: str,
        params: dict[str, Any] | None,
        session: Session,
        needs_token: bool,
    ) -> httpx.Response:
        headers: dict[str, str] = {}
        if needs_token:
            if not session.jwt:
                raise UntisUnavailable(
                    self.profile.name,
                    f"{path} needs a bearer token, which this tenant did not issue",
                )
            headers["Authorization"] = f"Bearer {session.jwt}"
            if session.tenant_id:
                headers["tenant-id"] = session.tenant_id

        # The cookie jar can be emptied by a redirect or a server-side invalidation; the
        # session id is authoritative, so it is re-applied before every request.
        apply_cookies(self._http, self.profile, session.session_id)

        try:
            return await self._http.get(
                f"{self.profile.server}{path}", params=params, headers=headers
            )
        except httpx.HTTPError as exc:
            raise UntisUnavailable(self.profile.name, f"{path}: {type(exc).__name__}") from exc

    def _http_error(self, path: str, response: httpx.Response) -> UntisError:
        detail = ""
        try:
            payload = response.json()
            if isinstance(payload, dict):
                detail = str(payload.get("errorMessage") or payload.get("errors") or "")
        except ValueError:
            detail = response.text[:200]

        if response.status_code == 403:
            return UntisError(
                self.profile.name,
                f"{path} is forbidden for this account. This is a property of the school's "
                f"account, not a temporary failure — do not retry it. {detail}".strip(),
            )
        return UntisUnavailable(
            self.profile.name, f"{path} answered HTTP {response.status_code}. {detail}".strip()
        )


class ClientRegistry:
    """One `UntisClient` per profile, created on demand and reused afterwards."""

    def __init__(self, config: Config, timeout: float = DEFAULT_TIMEOUT) -> None:
        self._config = config
        self._timeout = timeout
        self._clients: dict[str, UntisClient] = {}

    def for_profile(self, profile: ProfileConfig) -> UntisClient:
        client = self._clients.get(profile.name)
        if client is None:
            # Separate clients mean separate cookie jars: nothing is shared between tenants.
            client = UntisClient(profile, timeout=self._timeout)
            self._clients[profile.name] = client
        return client

    def cached_capabilities(self, profile: ProfileConfig) -> Capabilities | None:
        """What is already known, without logging in — for `list_profiles` (ADR-010)."""
        client = self._clients.get(profile.name)
        return client.capabilities if client else None

    async def aclose(self) -> None:
        for client in list(self._clients.values()):
            try:
                await client.aclose()
            except Exception:  # noqa: BLE001 - shutdown must not raise
                logger.debug("profile %r: error while closing", client.profile.name)
        self._clients.clear()
