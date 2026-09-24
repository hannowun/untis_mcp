"""The static token of ADR-006, accepted in two shapes: a bearer header, or a URL prefix.

A client that can set request headers sends `Authorization: Bearer <token>`. The Claude web
and mobile apps cannot — a custom connector there gets a URL and nothing else — so the same
token may instead be the first path segment of every URL:

    https://host:8448/<token>/mcp
    https://host:8448/<token>/api/profiles

This module makes the second shape *become* the first before anything else sees the request:
an ASGI middleware ahead of FastMCP's own authentication strips a matching prefix from the
path and puts the bearer header in place. Everything downstream — FastMCP's token
verification around the MCP endpoint, the REST layer's own check — keeps enforcing one
token in one way, and neither needs to know that a URL can carry it.

A request whose first segment is *not* the token is passed through untouched, which is what
lets the plain bearer shape keep working beside this one: it simply meets the ordinary 401.
"""

from __future__ import annotations

import logging
import secrets
from typing import Any

from fastmcp.server.auth.providers.jwt import StaticTokenVerifier
from starlette.middleware import Middleware

logger = logging.getLogger(__name__)

# The ASGI callable types, spelled loosely: this module is the only place they appear.
Scope = dict[str, Any]


class PathTokenMiddleware:
    """Turn `/<token>/rest` into `/rest` plus `Authorization: Bearer <token>`.

    The scope is rewritten in place rather than copied, deliberately: uvicorn's access log
    reads the path from the same dict after the response, so the token never reaches the log.
    """

    def __init__(self, app: Any, token: str) -> None:
        self.app = app
        self.token = token.encode()

    async def __call__(self, scope: Scope, receive: Any, send: Any) -> None:
        if scope["type"] == "http":
            self.rewrite(scope)
        await self.app(scope, receive, send)

    def rewrite(self, scope: Scope) -> None:
        segment, _, rest = scope["path"].lstrip("/").partition("/")
        # `compare_digest` on `str` insists on ASCII; bytes accept whatever a URL decoded to.
        if not segment or not secrets.compare_digest(segment.encode(), self.token):
            return

        # A trailing slash is dropped here rather than left to Starlette: its redirect from
        # `/mcp/` to `/mcp` is built from the rewritten path, so it would send the client
        # back to a URL without the token — and straight into the 401.
        scope["path"] = "/" + rest.rstrip("/")
        # `raw_path` is the undecoded original; the token is plain hex, so the prefix is
        # spelled identically there, and it can be cut instead of re-encoded.
        prefix = b"/" + self.token
        raw_path: bytes | None = scope.get("raw_path")
        if raw_path is not None and raw_path.startswith(prefix):
            scope["raw_path"] = raw_path[len(prefix) :].rstrip(b"/") or b"/"

        scope["headers"] = [
            (name, value) for name, value in scope["headers"] if name != b"authorization"
        ] + [(b"authorization", b"Bearer " + self.token)]


class TokenAuth(StaticTokenVerifier):
    """FastMCP's static-token verifier, with the URL shape accepted in front of it.

    FastMCP builds its HTTP app with the provider's middleware ahead of the routes and the
    `middleware=` argument of `http_app()` *behind* its own authentication — too late to
    rewrite a header the authentication has already read. The provider's own middleware list
    is the one hook that runs first, so the rewrite is prepended there.
    """

    def __init__(self, token: str) -> None:
        super().__init__(tokens={token: {"client_id": "untis-mcp"}})
        self.token = token

    def get_middleware(self) -> list:
        return [Middleware(PathTokenMiddleware, token=self.token), *super().get_middleware()]
