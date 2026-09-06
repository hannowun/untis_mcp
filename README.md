# untis-mcp

A private [MCP](https://modelcontextprotocol.io) server that makes **WebUntis** information
available to an AI assistant.

Both of my children attend different schools, and each school runs its own WebUntis tenant
with its own credentials. This server logs into all of them and exposes their data through
a handful of read-only MCP tools, so I can ask one assistant about timetables, absences and
school messages instead of logging into two portals.

> **Status:** early development. The requirements, design and architecture are written down
> in [`documents/requirements-and-design.md`](documents/requirements-and-design.md).
> The endpoints are verified against both real schools; profiles, the client layer, the two
> timetable tools and absences work. Appointments and messages are not implemented yet.

---

## Features

Each school/child is configured as a named **profile**. Every tool call names the profile it
addresses, so one server instance serves all children.

| Tool | Description |
|------|-------------|
| `list_profiles` | Lists the configured profiles (names and descriptions, no credentials). |
| `get_appointments` | Appointments and calendar entries stored in WebUntis. |
| `get_absences` | Recorded absences with time span, reason and excuse status. |
| `get_timetable_week` | The timetable for a given school week. |
| `get_timetable_day` | The timetable for a given day. |
| `get_messages` | Messages / messages-of-the-day for the account. |

All tools are **read-only** — the server never changes anything in WebUntis.

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
| `UNTIS_MCP_AUTH_TOKEN` | unset | Optional static bearer token required from clients. |
| `UNTIS_MCP_LOG_LEVEL` | `INFO` | Log verbosity (logs go to stderr). |

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

Or over HTTP:

```bash
UNTIS_MCP_TRANSPORT=http UNTIS_MCP_PORT=8000 uv run untis-mcp
```

So far it serves `list_profiles`, `get_timetable_day`, `get_timetable_week` and
`get_absences`; appointments and messages follow.

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

Build on Windows:

```powershell
./scripts/build.ps1 -Tag untis-mcp:latest -Platform linux/arm64
```

Deploy on the Raspberry Pi:

```bash
./scripts/deploy.sh
```

The configuration file is mounted at runtime and never baked into the image. See
[section 6 of the design document](documents/requirements-and-design.md#7-deployment) for
the registry and tarball transfer paths.

### Security note

The server holds credentials for all configured schools and performs **no authorization of
its own** — any client that can reach the HTTP port can read every profile. Bind it to your
LAN or VPN interface only, never port-forward it from the internet, and consider setting
`UNTIS_MCP_AUTH_TOKEN`.

---

## Documentation

- [Requirements, Design and Architecture](documents/requirements-and-design.md)
