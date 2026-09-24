"""A plain HTTP interface onto the same calls the MCP tools offer (ADR-012).

Not every client that wants a timetable speaks MCP. A shell script, a dashboard tile or a
home-automation rule wants to fetch a URL and read JSON, and this module gives them that
without a second implementation of anything: each route resolves to one registered tool and
calls it through `FastMCP.call_tool`, so the arguments, the defaults, the validation and the
result models are literally the tool's own. A tool that changes changes here too, and
`ROUTES` is checked against the registered tools at startup, so a tool added without a route
is a failure at boot rather than a silently missing endpoint.

The shape is REST-like rather than RPC-like: every call is a `GET`, because the whole server
is read-only (ADR-007); the profile is a path segment, because scoping every answer to one
child is the central property of this server (FR-04); the remaining tool arguments are query
parameters.

    GET /api/profiles/anna/homework?only_open=true

Routes registered here are *not* covered by the MCP transport's bearer authentication, which
FastMCP wraps around the MCP endpoint alone — so the same token is enforced in this module.
Adding a second interface must not open a door beside the one ADR-006 guards.
"""

from __future__ import annotations

import logging
import secrets
from collections.abc import Awaitable, Callable
from typing import Any

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError, ValidationError
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from untis_mcp.errors import (
    ConfigError,
    FeatureNotAvailable,
    ProfileNotFound,
    StudentAmbiguous,
    UntisAuthError,
    UntisError,
    UntisUnavailable,
)

logger = logging.getLogger(__name__)

API_PREFIX = "/api"

# The argument every data tool takes, and the one this interface reads from the path.
PROFILE_ARG = "profile"

# Tool name -> path below `API_PREFIX`. Spelled out rather than derived from the tool names,
# because "get_messages_of_day" is not a resource path; `check_routes` keeps the two halves
# in step. `{profile}` is taken from the URL and passed as the `profile` argument.
ROUTES: dict[str, str] = {
    "list_profiles": "/profiles",
    "get_timetable_day": "/profiles/{profile}/timetable/day",
    "get_timetable_week": "/profiles/{profile}/timetable/week",
    "get_absences": "/profiles/{profile}/absences",
    "get_appointments": "/profiles/{profile}/appointments",
    "get_homework": "/profiles/{profile}/homework",
    "get_messages": "/profiles/{profile}/messages",
    "get_messages_of_day": "/profiles/{profile}/messages-of-day",
}

# How a failure that reached us as a `ToolError` is reported over HTTP. The tool layer wraps
# every deliberate error of this server as `raise ToolError(...) from exc`, so the original
# is still attached and a status code can be chosen without reading the message. Subclasses
# come before their bases.
#
# `kind` is a stable token for clients that want to branch without parsing prose; the message
# stays the one the assistant would have been given.
ERROR_STATUS: tuple[tuple[type[BaseException], int, str], ...] = (
    (ProfileNotFound, 404, "unknown_profile"),
    # A permanent property of the school's account, not a fault and not worth retrying
    # (ADR-010) — which is what 501 says, and unlike 403 it cannot be mistaken for a
    # complaint about the caller's own token.
    (FeatureNotAvailable, 501, "feature_not_available"),
    (StudentAmbiguous, 500, "configuration_error"),
    (ConfigError, 500, "configuration_error"),
    (UntisAuthError, 502, "upstream_auth_failed"),
    (UntisUnavailable, 503, "upstream_unavailable"),
    (UntisError, 502, "upstream_error"),
)

UNAUTHORIZED = "this server requires a bearer token: send 'Authorization: Bearer <token>'."

TRUE_WORDS = {"true", "1", "yes", "on"}
FALSE_WORDS = {"false", "0", "no", "off"}


def check_routes(tools: list[str]) -> None:
    """Fail loudly when `ROUTES` and the registered tools have drifted apart."""
    unrouted = [name for name in tools if name not in ROUTES]
    unknown = [name for name in ROUTES if name not in tools]
    if unrouted or unknown:
        raise RuntimeError(
            "untis_mcp.rest.ROUTES is out of step with the registered tools: "
            f"no route for {unrouted}, route for unknown tool {unknown}"
        )


def error(status: int, kind: str, message: str, headers: dict[str, str] | None = None) -> Response:
    """The one error shape this interface returns."""
    return JSONResponse({"error": message, "kind": kind}, status_code=status, headers=headers)


def unauthorized() -> Response:
    return error(401, "unauthorized", UNAUTHORIZED, headers={"WWW-Authenticate": "Bearer"})


def status_for(exc: BaseException) -> tuple[int, str]:
    """Classify a failure by the exception the tool layer wrapped, not by its text."""
    cause = exc.__cause__
    for kind, status, token in ERROR_STATUS:
        if isinstance(cause, kind):
            return status, token
    # A `ToolError` the tool layer raised itself: a date that will not parse, a range that
    # runs backwards. Those are the caller's mistakes.
    return 400, "bad_request"


def authorized(request: Request, token: str | None) -> bool:
    """Check the same bearer token the MCP endpoint requires (ADR-006)."""
    if not token:
        return True
    scheme, _, presented = request.headers.get("authorization", "").partition(" ")
    if scheme.lower() != "bearer":
        return False
    return secrets.compare_digest(presented.strip(), token)


def accepted_arguments(schema: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """A tool's arguments minus `profile`, which this interface takes from the path."""
    properties = schema.get("properties") or {}
    return {name: spec for name, spec in properties.items() if name != PROFILE_ARG}


def scalar_types(spec: dict[str, Any]) -> set[str]:
    """The JSON types an argument accepts, ignoring the `null` half of an optional one."""
    if "anyOf" in spec:
        return {
            option.get("type")
            for option in spec["anyOf"]
            if isinstance(option, dict) and option.get("type") not in (None, "null")
        }
    declared = spec.get("type")
    return {declared} if declared and declared != "null" else set()


def coerce(name: str, raw: str, spec: dict[str, Any]) -> Any:
    """Turn one query parameter into the type the tool's schema asks for.

    Everything arrives as text, so `only_open=false` has to become a boolean before the tool
    sees it — otherwise the string "false" would be a perfectly true argument.
    """
    types = scalar_types(spec)
    if "boolean" in types:
        lowered = raw.strip().lower()
        if lowered in TRUE_WORDS:
            return True
        if lowered in FALSE_WORDS:
            return False
        raise ToolError(f"{name}={raw!r} is not a yes/no value. Use 'true' or 'false'.")
    if "integer" in types and "string" not in types:
        try:
            return int(raw.strip())
        except ValueError as exc:
            raise ToolError(f"{name}={raw!r} is not a whole number.") from exc
    return raw


def build_arguments(request: Request, schema: dict[str, Any]) -> dict[str, Any]:
    """Assemble the tool arguments from the path and the query string.

    An unknown parameter is refused rather than ignored: a typo in `only_unexcused` would
    otherwise be answered with the unfiltered list, which looks exactly like the truth.
    """
    accepted = accepted_arguments(schema)
    arguments: dict[str, Any] = {}

    if PROFILE_ARG in request.path_params:
        arguments[PROFILE_ARG] = request.path_params[PROFILE_ARG]

    for name in request.query_params:
        if name not in accepted:
            listed = ", ".join(sorted(accepted)) or "none"
            raise ToolError(f"unknown parameter {name!r}; this endpoint accepts: {listed}.")
        raw = request.query_params[name]
        # `?date=` is what a shell script writes when its variable happens to be empty. Read
        # that as "not given", so the tool's documented default applies instead of failing.
        if raw == "":
            continue
        arguments[name] = coerce(name, raw, accepted[name])

    return arguments


def unwrap(result: Any, schema: dict[str, Any] | None) -> Any:
    """Return the tool's own JSON, undoing the envelope MCP needs but REST does not.

    A tool whose result is not an object — `list_profiles` returns a list — is wrapped by
    FastMCP into `{"result": [...]}`, because MCP structured content has to be an object.
    Over HTTP a list is a perfectly good body, so the envelope comes off again.
    """
    if schema and schema.get("x-fastmcp-wrap-result") and isinstance(result, dict):
        return result.get("result")
    return result


def describe_parameters(schema: dict[str, Any]) -> list[dict[str, Any]]:
    """Describe a tool's arguments for the index, in the terms an HTTP caller needs."""
    required = set(schema.get("required") or [])
    return [
        {
            "name": name,
            "type": "/".join(sorted(scalar_types(spec))) or "string",
            "required": name in required,
            "default": spec.get("default"),
        }
        for name, spec in accepted_arguments(schema).items()
    ]


def make_handler(
    mcp: FastMCP, tool_name: str, token: str | None
) -> Callable[[Request], Awaitable[Response]]:
    """One endpoint: check the token, translate the request, call the tool, return its JSON."""

    async def handler(request: Request) -> Response:
        if not authorized(request, token):
            return unauthorized()

        tool = await mcp.get_tool(tool_name)
        if tool is None:  # pragma: no cover - check_routes rules this out at startup
            return error(500, "internal_error", f"tool {tool_name!r} is not registered.")

        try:
            arguments = build_arguments(request, tool.parameters)
        except ToolError as exc:
            return error(400, "bad_request", str(exc))

        try:
            result = await mcp.call_tool(tool_name, arguments)
        except ValidationError as exc:
            # An argument the tool's own schema rejected — a wrong type, a missing profile.
            return error(400, "bad_request", str(exc))
        except ToolError as exc:
            status, kind = status_for(exc)
            logger.info("GET %s -> %d (%s)", request.url.path, status, kind)
            return error(status, kind, str(exc))
        except Exception:
            # Anything not deliberately raised is a bug here, not news for the caller: log
            # it with its traceback and say nothing that could leak a credential.
            logger.exception("GET %s failed unexpectedly", request.url.path)
            return error(500, "internal_error", "the server failed to answer this request.")

        return JSONResponse(unwrap(result.structured_content, tool.output_schema))

    handler.__name__ = f"rest_{tool_name}"
    return handler


def make_index(mcp: FastMCP, token: str | None) -> Callable[[Request], Awaitable[Response]]:
    """`GET /api`: what this interface offers, in enough detail to call it from a shell."""

    async def index(request: Request) -> Response:
        if not authorized(request, token):
            return unauthorized()

        tools = {tool.name: tool for tool in await mcp.list_tools()}
        endpoints = [
            {
                "method": "GET",
                "path": f"{API_PREFIX}{path}",
                "tool": name,
                "description": tools[name].description or "",
                "parameters": describe_parameters(tools[name].parameters),
            }
            for name, path in ROUTES.items()
            if name in tools
        ]

        return JSONResponse(
            {
                "name": mcp.name,
                "version": mcp.version,
                "description": (
                    "Read-only HTTP interface onto the same calls the MCP tools offer. "
                    "Dates are ISO-8601, booleans are 'true' or 'false', and an omitted "
                    "parameter takes the default described with it. Every answer names the "
                    "profile it was produced for."
                ),
                "authentication": "bearer" if token else "none",
                "endpoints": endpoints,
            }
        )

    return index


def register_rest_api(mcp: FastMCP, auth_token: str | None = None) -> None:
    """Register the REST routes on `mcp`.

    Only reachable under an HTTP transport; over stdio there is no HTTP server to serve them,
    and registering them there costs nothing. Whether they cover every tool is checked once
    the server starts, where the tool list can be read (`check_routes`).
    """
    mcp.custom_route(API_PREFIX, methods=["GET"], name="rest_index")(make_index(mcp, auth_token))
    for name, path in ROUTES.items():
        mcp.custom_route(f"{API_PREFIX}{path}", methods=["GET"], name=f"rest_{name}")(
            make_handler(mcp, name, auth_token)
        )

    logger.info("REST interface registered at %s with %d endpoint(s)", API_PREFIX, len(ROUTES))
