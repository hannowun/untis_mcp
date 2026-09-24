"""The REST interface (ADR-012): the same answers as the tools, over plain HTTP.

The app is driven through `httpx.ASGITransport`, so these run against the real Starlette
application FastMCP builds — the routes, the query parsing and the status codes are the ones
a `curl` on the Raspberry Pi would meet — without binding a port.

What matters most here is *parity*: the REST body must be what the MCP tool returned, not a
second rendering of the same data that could drift away from it.
"""

from __future__ import annotations

import datetime as dt

import httpx
import pytest
from fastmcp import Client

from tests.fake_untis import (
    TENANT_WITHOUT_MESSAGES,
    VIEWS_WITHOUT_MESSAGES,
    FakeUntis,
)
from untis_mcp.config import Config
from untis_mcp.rest import ROUTES, check_routes
from untis_mcp.server import create_server
from untis_mcp.untis.client import (
    ABSENCES,
    HOMEWORK,
    TIMETABLE_ENTRIES,
    ClientRegistry,
    UntisClient,
)

CONFIG = Config.model_validate(
    {
        "profiles": [
            {
                "name": "anna",
                "description": "Anna, Gymnasium Musterstadt",
                "server": "https://ajax.webuntis.com",
                "school": "gym-musterstadt",
                "username": "anna.mustermann",
                "password": "anna-plaintext",
            }
        ]
    }
)

# The fixtures were captured in September 2026; ask for the span they cover.
START, END = dt.date(2026, 9, 1), dt.date(2026, 9, 30)
SPAN = f"start={START.isoformat()}&end={END.isoformat()}"


def build(fake: FakeUntis | None = None, **server_kwargs):
    """A server whose one profile talks to a fake tenant, plus the fake itself."""
    fake = fake or (
        FakeUntis()
        .serve_fixture(HOMEWORK, "homework.json")
        .serve_fixture(ABSENCES, "absences.json")
        .serve_fixture(TIMETABLE_ENTRIES, "timetable_entries.json")
    )
    registry = ClientRegistry(CONFIG)
    registry._clients["anna"] = UntisClient(CONFIG.get("anna"), http=fake.http_client())
    return create_server(CONFIG, registry=registry, **server_kwargs), fake


def http(server) -> httpx.AsyncClient:
    """An HTTP client speaking to the server's ASGI app in-process."""
    app = server.http_app()
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://untis-mcp.test"
    )


@pytest.fixture
def server():
    return build()[0]


# ------------------------------------------------------------------------------ discovery


async def test_the_index_describes_every_endpoint(server):
    """`GET /api` is the documentation a shell user gets without reading the README."""
    async with http(server) as client:
        response = await client.get("/api")

    assert response.status_code == 200
    index = response.json()
    assert index["name"] == "untis-mcp"
    assert index["authentication"] == "none"

    by_tool = {endpoint["tool"]: endpoint for endpoint in index["endpoints"]}
    assert set(by_tool) == set(ROUTES)

    homework = by_tool["get_homework"]
    assert homework["method"] == "GET"
    assert homework["path"] == "/api/profiles/{profile}/homework"
    # The description is the tool's own, so the two interfaces cannot document differently.
    assert "due date" in homework["description"]

    parameters = {item["name"]: item for item in homework["parameters"]}
    # `profile` lives in the path, so it is not listed as a query parameter.
    assert "profile" not in parameters
    assert parameters["only_open"]["type"] == "boolean"
    assert parameters["only_open"]["default"] is False
    assert parameters["start"]["required"] is False


async def test_every_registered_tool_has_a_route(server):
    """The anti-drift guard `create_server` runs at startup, checked directly."""
    check_routes([tool.name for tool in await server.list_tools()])


def test_check_routes_refuses_a_tool_without_a_route():
    with pytest.raises(RuntimeError, match="get_grades"):
        check_routes([*ROUTES, "get_grades"])


def test_check_routes_refuses_a_route_without_a_tool():
    with pytest.raises(RuntimeError, match="get_homework"):
        check_routes([name for name in ROUTES if name != "get_homework"])


# --------------------------------------------------------------------------------- answers


async def test_profiles_are_returned_as_a_plain_list(server):
    """MCP wraps a list result in an envelope; over HTTP the list is the body."""
    async with http(server) as client:
        response = await client.get("/api/profiles")

    assert response.status_code == 200
    profiles = response.json()
    assert isinstance(profiles, list)
    assert [item["name"] for item in profiles] == ["anna"]
    assert "anna-plaintext" not in response.text


async def test_the_body_is_exactly_what_the_tool_returned(server):
    """Parity (ADR-012): one implementation, two front doors."""
    async with http(server) as client:
        response = await client.get(f"/api/profiles/anna/homework?{SPAN}")

    async with Client(server) as mcp_client:
        result = await mcp_client.call_tool(
            "get_homework", {"profile": "anna", "start": START.isoformat(), "end": END.isoformat()}
        )

    assert response.status_code == 200
    assert response.json() == result.structured_content
    assert response.json()["profile"] == "anna"


async def test_a_timetable_day_is_served_under_its_own_path(server):
    async with http(server) as client:
        response = await client.get("/api/profiles/anna/timetable/day?date=2026-09-07")

    assert response.status_code == 200
    body = response.json()
    assert body["profile"] == "anna"
    assert body["start"] == body["end"] == "2026-09-07"


async def test_a_boolean_query_parameter_reaches_the_tool_as_a_boolean():
    """`only_open=false` must not arrive as the very much true string "false"."""
    server, _ = build()
    async with http(server) as client:
        everything = await client.get(f"/api/profiles/anna/homework?{SPAN}&only_open=false")
        open_only = await client.get(f"/api/profiles/anna/homework?{SPAN}&only_open=true")

    assert len(open_only.json()["homework"]) < len(everything.json()["homework"])
    assert all(not item["completed"] for item in open_only.json()["homework"])


async def test_an_empty_query_value_means_the_default(server):
    """What a shell script writes when its variable is unset: ?date= — not an error."""
    async with http(server) as client:
        response = await client.get("/api/profiles/anna/timetable/day?date=")

    assert response.status_code == 200
    assert response.json()["start"] == dt.date.today().isoformat()


async def test_the_week_can_be_asked_for_by_calendar_week(server):
    async with http(server) as client:
        response = await client.get("/api/profiles/anna/timetable/week?year=2026&week=37")

    assert response.status_code == 200
    assert response.json()["start"] == "2026-09-07"


# ---------------------------------------------------------------------------------- errors


async def test_an_unknown_profile_is_not_found(server):
    async with http(server) as client:
        response = await client.get("/api/profiles/charlie/absences")

    assert response.status_code == 404
    body = response.json()
    assert body["kind"] == "unknown_profile"
    # The message names the profiles that do exist, exactly as the tool would (FR-04).
    assert "'anna'" in body["error"]


async def test_an_unparseable_date_is_a_bad_request(server):
    async with http(server) as client:
        response = await client.get("/api/profiles/anna/timetable/day?date=7.9.2026")

    assert response.status_code == 400
    assert response.json()["kind"] == "bad_request"
    assert "ISO-8601" in response.json()["error"]


async def test_a_misspelled_parameter_is_refused_rather_than_ignored(server):
    """Silently answering the unfiltered list would look exactly like the truth."""
    async with http(server) as client:
        response = await client.get(f"/api/profiles/anna/homework?{SPAN}&only_opne=true")

    assert response.status_code == 400
    assert "only_open" in response.json()["error"]


async def test_a_boolean_that_is_not_a_boolean_is_a_bad_request(server):
    async with http(server) as client:
        response = await client.get(f"/api/profiles/anna/homework?{SPAN}&only_open=vielleicht")

    assert response.status_code == 400
    assert "true" in response.json()["error"]


async def test_a_feature_the_school_forbids_is_reported_as_permanent():
    """ADR-010 over HTTP: 501, so a client can stop asking rather than retry."""
    server, _ = build(
        FakeUntis(permissions=list(TENANT_WITHOUT_MESSAGES), views=list(VIEWS_WITHOUT_MESSAGES))
    )
    async with http(server) as client:
        response = await client.get("/api/profiles/anna/messages")

    assert response.status_code == 501
    assert response.json()["kind"] == "feature_not_available"
    assert "do not retry" in response.json()["error"]


async def test_an_unreachable_school_is_a_bad_gateway_not_a_bad_request():
    server, _ = build(FakeUntis(unreachable=True))
    async with http(server) as client:
        response = await client.get("/api/profiles/anna/absences")

    assert response.status_code == 503
    assert response.json()["kind"] == "upstream_unavailable"


# ------------------------------------------------------------------------------------ auth


async def test_the_bearer_token_guards_the_rest_interface_too():
    """The token of ADR-006 wraps the MCP endpoint only — the REST routes check it here."""
    server, _ = build(auth_token="s3kret")
    async with http(server) as client:
        without = await client.get("/api/profiles/anna/absences")
        wrong = await client.get(
            "/api/profiles/anna/absences", headers={"Authorization": "Bearer nope"}
        )
        right = await client.get(
            f"/api/profiles/anna/absences?{SPAN}", headers={"Authorization": "Bearer s3kret"}
        )

    assert without.status_code == 401
    assert without.headers["www-authenticate"] == "Bearer"
    assert without.json()["kind"] == "unauthorized"
    assert wrong.status_code == 401
    assert right.status_code == 200


async def test_the_index_is_behind_the_token_as_well():
    server, _ = build(auth_token="s3kret")
    async with http(server) as client:
        assert (await client.get("/api")).status_code == 401
        allowed = await client.get("/api", headers={"Authorization": "Bearer s3kret"})

    assert allowed.status_code == 200
    assert allowed.json()["authentication"] == "bearer"


# --------------------------------------------------------------------------- switched off


async def test_the_rest_interface_can_be_left_out():
    """`UNTIS_MCP_REST=off`: an MCP-only server has no /api at all."""
    server, _ = build(rest_api=False)
    async with http(server) as client:
        assert (await client.get("/api")).status_code == 404
        assert (await client.get("/api/profiles")).status_code == 404


# ------------------------------------------------------------------------ token in the URL


async def test_the_token_may_be_the_first_path_segment_instead_of_a_header():
    """A client that cannot send headers — a Claude web connector — carries the token in
    the URL, and reaches the very same route as one that sends the bearer header."""
    server, _ = build(auth_token="s3kret")
    async with http(server) as client:
        in_path = await client.get(f"/s3kret/api/profiles/anna/absences?{SPAN}")
        in_header = await client.get(
            f"/api/profiles/anna/absences?{SPAN}", headers={"Authorization": "Bearer s3kret"}
        )
        index = await client.get("/s3kret/api")

    assert in_path.status_code == 200
    assert in_path.json() == in_header.json()
    assert index.status_code == 200


async def test_a_trailing_slash_after_the_path_token_url_does_not_lose_the_token():
    """Starlette answers `/mcp/` with a redirect to `/mcp`, built from the path it sees —
    which, after the rewrite, no longer carries the token. A connector URL pasted with a
    trailing slash would follow that redirect into the 401, so the slash is dropped first."""
    server, _ = build(auth_token="s3kret")
    async with http(server) as client:
        index = await client.get("/s3kret/api/")
        profiles = await client.get("/s3kret/api/profiles/")

    assert index.status_code == 200
    assert profiles.status_code == 200


async def test_a_wrong_or_misplaced_path_token_does_not_open_anything():
    server, _ = build(auth_token="s3kret")
    async with http(server) as client:
        wrong = await client.get("/nope/api/profiles")
        # A bearer header the path token would overwrite: the path wins only when it matches.
        wrong_with_bad_header = await client.get(
            "/nope/api/profiles", headers={"Authorization": "Bearer nope"}
        )
        # The token has to be the *first* segment; deeper in the URL it is just a path.
        deeper = await client.get("/api/s3kret/profiles")

    # No matching prefix is stripped, so "/nope/api/profiles" is simply no route.
    assert wrong.status_code == 404
    assert wrong_with_bad_header.status_code == 404
    assert deeper.status_code == 404


async def test_the_path_token_does_not_override_a_configured_header():
    """The path only ever *adds* the right token; it cannot be used to smuggle in a wrong
    header, and a wrong header beside the right path is still the right token."""
    server, _ = build(auth_token="s3kret")
    async with http(server) as client:
        response = await client.get(
            "/s3kret/api/profiles", headers={"Authorization": "Bearer stale"}
        )

    assert response.status_code == 200


async def test_the_mcp_endpoint_accepts_the_path_token_too():
    """The MCP endpoint is guarded by FastMCP's own middleware, not by rest.py — so the
    rewrite has to happen before *that* sees the request, on `/<token>/mcp`."""
    server, _ = build(auth_token="s3kret")
    initialize = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "test", "version": "0"},
        },
    }
    headers = {"Accept": "application/json, text/event-stream"}

    app = server.http_app()
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://untis-mcp.test"
        ) as client,
    ):
        bare = await client.post("/mcp", json=initialize, headers=headers)
        in_path = await client.post("/s3kret/mcp", json=initialize, headers=headers)
        in_header = await client.post(
            "/mcp", json=initialize, headers={**headers, "Authorization": "Bearer s3kret"}
        )

    assert bare.status_code == 401
    assert in_path.status_code == 200, in_path.text
    assert in_header.status_code == 200, in_header.text
    assert "untis-mcp" in in_path.text


async def test_without_a_token_no_prefix_is_recognised():
    """No token configured means no rewriting at all — nothing hidden behind a random path."""
    server, _ = build()
    async with http(server) as client:
        assert (await client.get("/api/profiles")).status_code == 200
        assert (await client.get("/anything/api/profiles")).status_code == 404
