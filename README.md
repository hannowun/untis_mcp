# untis-mcp

A private [MCP](https://modelcontextprotocol.io) server that makes **WebUntis** information
available to an AI assistant.

Both of my children attend different schools, and each school runs its own WebUntis tenant
with its own credentials. This server logs into all of them and exposes their data through
a handful of read-only MCP tools, so I can ask one assistant about timetables, absences and
school messages instead of logging into two portals.

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
| `get_timetable_week` | The timetable for a given school week. |
| `get_timetable_day` | The timetable for a given day. |
| `get_messages` | Messages sent to the account through the WebUntis message centre. |
| `get_messages_of_day` | The school's announcements for a day. |

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

All read-only tools are implemented; the container setup is what remains.

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
| `UNTIS_MCP_AUTH_TOKEN` | empty | Bearer token clients must present. |

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
its own** — any client that can reach the HTTP port can read every profile. `UNTIS_MCP_BIND`
in `.env` is therefore the real access control: point it at your LAN or VPN interface only,
never port-forward it from the internet, and set `UNTIS_MCP_AUTH_TOKEN` as soon as it is
anything other than `127.0.0.1`.

---

## Documentation

- [Requirements, Design and Architecture](documents/requirements-and-design.md)
