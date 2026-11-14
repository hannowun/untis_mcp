# untis-mcp

A self-hosted [MCP](https://modelcontextprotocol.io) server that makes **WebUntis**
information available to an AI assistant.

It is built for families whose children attend different schools, where each school runs
its own WebUntis tenant with its own credentials. The server logs into all of them and
exposes their data through a handful of read-only MCP tools, so one assistant can answer
questions about timetables, absences, homework and school messages instead of you logging
into several portals. The same calls are available as a plain HTTP/JSON API for everything
that does not speak MCP.

> **Status:** early development. The requirements, design and architecture are written down
> in [`documents/requirements-and-design.md`](documents/requirements-and-design.md).
> The endpoints are verified against both real schools, every tool works, and the container
> builds and runs for both `linux/amd64` and `linux/arm64`.

---

## Features

Each school/child is configured as a named **profile**. Every tool call names the profile it
addresses, so one server instance serves all children.

| Tool | Description |
|------|-------------|
| `list_profiles` | Lists the configured profiles (names and descriptions, no credentials). |
| `get_appointments` | Appointments and calendar entries stored in WebUntis. |
| `get_absences` | Recorded absences with time span, reason and excuse status. |
| `get_homework` | Homework set in the class register, by due date. |
| `get_timetable_week` | The timetable for a given school week. |
| `get_timetable_day` | The timetable for a given day. |
| `get_messages` | Messages sent to the account through the WebUntis message centre. |
| `get_messages_of_day` | The school's announcements for a day. |

All tools are **read-only** — the server never changes anything in WebUntis. Every one of
them is also reachable over plain HTTP; see [REST interface](#rest-interface).

---

## Configuration

Profiles are defined in a TOML file, by default `config/profiles.toml`. Override the
location with the `UNTIS_MCP_CONFIG` environment variable.

```toml
[[profiles]]
name        = "anna"
description = "Anna, Gymnasium Musterstadt"
server      = "https://ajax.webuntis.com"
school      = "gym-musterstadt"
username    = "anna.mustermann"
password    = "secret"

[[profiles]]
name         = "ben"
description  = "Ben, Realschule Beispieldorf"
server       = "https://herakles.webuntis.com"
school       = "rs-beispieldorf"
username     = "ben.mustermann"
password_env = "UNTIS_BEN_PASSWORD"   # read the password from an environment variable
```

**This file contains plaintext credentials.** Keep it out of version control, restrict its
file permissions, and mount it read-only into the container.

### Environment variables

| Variable | Default | Purpose |
|----------|---------|---------|
| `UNTIS_MCP_CONFIG` | `config/profiles.toml` | Path to the profile configuration. |
| `UNTIS_MCP_TRANSPORT` | `http` in the container, `stdio` locally | MCP transport. |
| `UNTIS_MCP_HOST` | `127.0.0.1` | Bind address for the HTTP transport. |
| `UNTIS_MCP_PORT` | `8000` | Port for the HTTP transport. |
| `UNTIS_MCP_AUTH_TOKEN` | unset | Optional static token required from clients, on both interfaces — as a bearer header or as the first URL segment. |
| `UNTIS_MCP_REST` | `on` | Serve the REST interface under `/api` beside the MCP endpoint. |
| `UNTIS_MCP_LOG_LEVEL` | `INFO` | Log verbosity (logs go to stderr). |

---

## REST interface

Under the HTTP transport the server also answers plain `GET` requests under `/api` — the
same calls as the tools, the same results, the same optional bearer token, on the same port.
It exists for the clients that do not speak MCP: a shell script, a dashboard tile, a
home-automation rule. Set `UNTIS_MCP_REST=off` to serve MCP alone.

```bash
curl -s "http://localhost:8000/api/profiles/anna/timetable/day?date=2026-09-07"
```

| Endpoint | Tool | Query parameters |
|----------|------|------------------|
| `GET /api` | — | (an index of everything below, with the tools' own descriptions) |
| `GET /api/profiles` | `list_profiles` | — |
| `GET /api/profiles/{profile}/timetable/day` | `get_timetable_day` | `date` |
| `GET /api/profiles/{profile}/timetable/week` | `get_timetable_week` | `week_of`, `year`, `week` |
| `GET /api/profiles/{profile}/absences` | `get_absences` | `start`, `end`, `only_unexcused` |
| `GET /api/profiles/{profile}/appointments` | `get_appointments` | `start`, `end` |
| `GET /api/profiles/{profile}/homework` | `get_homework` | `start`, `end`, `only_open` |
| `GET /api/profiles/{profile}/messages` | `get_messages` | `unread_only`, `limit` |
| `GET /api/profiles/{profile}/messages-of-day` | `get_messages_of_day` | `date` |

Dates are ISO-8601 (`2026-09-07`), booleans are `true`/`false`, and every parameter is
optional with the default documented in `GET /api`. An empty value (`?date=`) means the
default, so a shell script need not special-case an unset variable. A misspelled parameter
is refused rather than silently ignored.

With a token set, both interfaces require it:

```bash
curl -s -H "Authorization: Bearer $UNTIS_MCP_AUTH_TOKEN" "http://localhost:8000/api/profiles/anna/homework?only_open=true"
```

A client that cannot send headers may put the same token in front of the path instead —
`/<token>/api/...` and `/<token>/mcp` reach the very same routes:

```bash
curl -s "http://localhost:8000/$UNTIS_MCP_AUTH_TOKEN/api/profiles/anna/homework?only_open=true"
```

Failures come back as `{"error": "...", "kind": "..."}` with a status that says what to do
about them:

| Status | `kind` | Meaning |
|--------|--------|---------|
| 400 | `bad_request` | A bad argument — an unparseable date, an unknown parameter. |
| 401 | `unauthorized` | The token is missing or wrong (a wrong token *in the URL* is a 404: no prefix is stripped, so the path simply does not exist). |
| 404 | `unknown_profile` | No such profile; the message lists the configured ones. |
| 501 | `feature_not_available` | This school's account is not permitted to use this call. Permanent — do not retry. |
| 502 | `upstream_auth_failed` | The login at the school failed. |
| 503 | `upstream_unavailable` | The school's server could not be reached. |

---

## Development

Requires [uv](https://docs.astral.sh/uv/) and Python 3.13.

```bash
uv sync
```

Run the tests:

```bash
uv run pytest
```

Lint and format:

```bash
uv run ruff check . && uv run ruff format .
```

Run the server over stdio (what an MCP client on the same machine launches):

```bash
uv run untis-mcp
```

Or over HTTP, which also serves the REST interface on `http://127.0.0.1:8000/api`:

```bash
UNTIS_MCP_TRANSPORT=http UNTIS_MCP_PORT=8000 uv run untis-mcp
```

### Protocol spike

Every WebUntis endpoint this project relies on is undocumented and reverse-engineered, so
step 0 of the implementation order is to verify them against the real school tenants before
any parsing code is written.

```bash
uv run python scripts/probe_endpoints.py
```

It logs into every configured profile, walks the endpoint map of
[section 5.2](documents/requirements-and-design.md#52-endpoint-map), and writes a report
plus the raw payloads to `probe-output/<run>/`. Each probe is reported as `ok`, `ok (empty)`,
`http 404`, `denied (...)` or a transport error; endpoints marked `?` are guesses, where a
404 is an expected outcome rather than a bug.

The output contains real names, timetables and messages. It is git-ignored — anonymise the
payloads before committing any of them as test fixtures.

---

## Deployment

The server is built as a container on a Windows/amd64 machine and runs on a Raspberry Pi
(arm64). A single `docker buildx` build covers both architectures.

### On the Pi: `.env`

Everything about the deployment that is not the same on every machine lives in a `.env`
file next to `compose.yaml` — above all **which interface and port the server is reachable
on**. It is git-ignored and read by both `compose.yaml` and `scripts/deploy.sh`, which
refuses to start without it.

```bash
cp .env.example .env && chmod 600 .env
```

| Variable | Default | Purpose |
|----------|---------|---------|
| `UNTIS_MCP_BIND` | `127.0.0.1` | Host interface the port is published on: the loopback, one address of the Pi, or `0.0.0.0` for all of them. |
| `UNTIS_MCP_EXPOSED_PORT` | `8000` | Host port clients connect to. |
| `UNTIS_MCP_PORT` | `8000` | Port the server listens on inside the container. |
| `UNTIS_MCP_IMAGE` | `untis-mcp:latest` | Image to run — a local tag or a registry reference. |
| `UNTIS_MCP_RUN_AS` | `1000:1000` | uid:gid the server runs as. Must be able to read `config/profiles.toml`, which a bind mount hands over with its host ownership intact — `stat -c '%u:%g' config/profiles.toml` prints the right value. |
| `TZ` | `Europe/Berlin` | Timezone the school day is interpreted in. |
| `UNTIS_MCP_LOG_LEVEL` | `INFO` | Log verbosity. |
| `UNTIS_MCP_AUTH_TOKEN` | empty | Token clients must present, on both interfaces, as a header or in the URL. |
| `UNTIS_MCP_REST` | `on` | Serve the REST interface under `/api`; `off` serves MCP alone. |

Any other variable in the file is passed into the container as well, which is how a profile
using `password_env` gets its password.

`deploy.sh` checks all of this before it starts anything: that the required variables are
set, that the ports are port numbers, that `UNTIS_MCP_BIND` is an address this machine
actually has — it lists the available ones if not — that `UNTIS_MCP_RUN_AS` can read the
profile configuration, and that every `password_env` named in `profiles.toml` has a value.
It warns when the binding reaches past the loopback without a token set.

### Build and deploy

Build on Windows — cross-builds for the Pi and writes a tarball:

```powershell
./scripts/build.ps1
```

Copy it across, then on the Raspberry Pi:

```bash
./scripts/deploy.sh --tarball ~/untis-mcp/untis-mcp-arm64.tar
```

With a registry instead of a tarball:

```powershell
./scripts/build.ps1 -Tag ghcr.io/you/untis-mcp:latest -Output registry
```

```bash
./scripts/deploy.sh --pull
```

To build on the Pi itself instead — the fallback if emulated cross-builds ever break:

```bash
./scripts/deploy.sh --build
```

The configuration file is mounted at runtime and never baked into the image. See
[section 6 of the design document](documents/requirements-and-design.md#7-deployment) for
the registry and tarball transfer paths.

### Security note

The server holds credentials for all configured schools and performs **no authorization of
its own** — any client that can reach the HTTP port can read every profile, through the MCP
endpoint or through `/api`. `UNTIS_MCP_BIND`
in `.env` is therefore the real access control: point it at your LAN or VPN interface only,
never port-forward it from the internet, and set `UNTIS_MCP_AUTH_TOKEN` as soon as it is
anything other than `127.0.0.1`.

### Connecting a client

With a token set, a client presents it in one of two ways:

- **As a bearer header** — Claude Desktop and Claude Code, `curl`, anything that can set
  request headers. For Claude Desktop, `mcp-remote` passes it along:

  ```json
  {
    "mcpServers": {
      "untis": {
        "command": "npx",
        "args": ["-y", "mcp-remote", "https://host:port/mcp", "--header", "Authorization:${AUTH_HEADER}"],
        "env": { "AUTH_HEADER": "Bearer <token>" }
      }
    }
  }
  ```

- **In the URL** — a custom connector in the Claude web and mobile apps is given a URL and
  nothing else (it authenticates by OAuth otherwise, which this server does not offer). Enter
  `https://host:port/<token>/mcp` as the connector's URL; the server strips the prefix and
  treats the request as if the header had been sent.

The URL shape makes the link itself the secret: whoever has it has the data. Generate the
token with `openssl rand -hex 32`, never paste the link into a chat, and keep it out of the
logs of any reverse proxy in front of the server — the server's own access log never sees it.

---

## Documentation

- [Requirements, Design and Architecture](documents/requirements-and-design.md)

---

## Disclaimer

This is an independent project. It is not affiliated with, endorsed by or supported by
Untis GmbH; "Untis" and "WebUntis" are their trademarks. The server talks to the same web
interface the WebUntis browser client uses, which is not a documented public API and may
change without notice. Use it at your own risk, only with accounts you are entitled to use,
and within the terms of use your school sets for its WebUntis access.

## License

[MIT](LICENSE) © 2026 Hanno Wunderlich
