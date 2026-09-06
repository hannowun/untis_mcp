# untis-mcp — Requirements, Design and Architecture

Status: draft (interview 2026-09-04, protocol research 2026-09-05,
verified against both tenants by the protocol spike 2026-09-06)
Owner: Hanno Wunderlich
Repository: `untis-mcp`

---

## 1. Purpose and context

Two children attend two different schools. Both schools publish information to parents
through the **WebUntis** platform, but each school runs its own WebUntis tenant with its
own server, school identifier and parent credentials.

`untis-mcp` is a **private MCP (Model Context Protocol) server** that exposes the
information stored in those WebUntis tenants to an AI assistant, so that questions like
"what does my daughter have in school tomorrow?" or "are there unexcused absences?" can be
answered without manually logging into two separate web portals.

### 1.1 Goals

- Read-only access to WebUntis data for **multiple children across multiple schools**.
- One MCP server instance serves **all** configured children.
- Credentials are managed centrally in a configuration file as named **profiles**.
- Every tool call explicitly states which profile it addresses.
- Runs as a container, built on a Windows/amd64 developer machine and deployed to a
  Raspberry Pi (ARM).

### 1.2 Non-goals

- Not a public or multi-tenant service. This is a **private** server for one household.
- No write operations to WebUntis (no excusing absences, no booking appointments, no
  sending messages). Read-only by design — see [ADR-007](#adr-007-read-only-scope).
- No user management and no per-user authorization inside the server. Whoever can reach
  the server can read all profiles.
- No general-purpose WebUntis API wrapper for third parties; the surface is exactly the
  five use cases below.

---

## 2. Functional requirements

### 2.1 Profiles

| ID | Requirement |
|----|-------------|
| FR-01 | Credentials for each school/child are stored as named profiles in a configuration file. |
| FR-02 | A profile carries at least: profile name, WebUntis server, school name, username, password, and a human-readable description (e.g. which child it belongs to). |
| FR-03 | Every data tool takes a mandatory `profile` argument identifying the profile to use. |
| FR-04 | An unknown profile name produces a clear error that lists the valid profile names. |
| FR-05 | The server can enumerate the available profiles so the AI can discover them without being told. |
| FR-09 | Each profile reports which tools it actually supports, derived from the account's own permissions at login (ADR-010). |

FR-05 is an addition on top of the five tools named in the interview: without it the model
has to guess profile names. It is implemented as a sixth tool, `list_profiles`, which
returns names and descriptions **but never credentials**.

FR-09 followed from the protocol spike: the two schools grant different permissions, so one
child's messages are readable and the other's are not (section 5.4). Reporting that up front
is better than letting the assistant discover it through a failed call.

### 2.2 Tools

All tools are read-only. All of them take `profile` as their first argument. Dates are
ISO-8601 (`YYYY-MM-DD`) strings on the wire.

| # | Tool (MCP name) | Interview name | Arguments | Returns |
|---|-----------------|----------------|-----------|---------|
| 1 | `get_appointments` | "Meine Termine" | `profile`, optional `start`, `end` | School events for the given range, from the timetable feed's `EVENT` entries plus any calendar entries (ADR-009). Defaults to a sensible upcoming window. |
| 2 | `get_absences` | "Abwesenheiten" | `profile`, optional `start`, `end`, optional `only_unexcused` | Recorded absences with time span, reason and excuse status. |
| 3 | `get_timetable_week` | "Stundenplan Woche" | `profile`, optional `week_of` (any date inside the week) or `year` + `week` | The timetable for the requested school week, grouped by day. |
| 4 | `get_timetable_day` | "Stundenplan Tag" | `profile`, optional `date` (default: today) | The timetable for a single day. |
| 5 | `get_messages` | "Mitteilungen" | `profile`, optional `unread_only`, optional `limit` | Messages / messages-of-the-day addressed to the account. |
| 6 | `list_profiles` | — | none | Configured profile names, descriptions and supported tools (no secrets). |

Common behaviour for all tools:

- Missing date arguments resolve to defaults documented in the tool description, because
  the model frequently calls tools without optional arguments.
- Results are returned as structured, JSON-serialisable objects (FastMCP structured
  output), not as pre-rendered text, so the assistant can reason over individual fields.
- Lesson entries include at minimum date, start/end time, subject, teacher(s), room(s),
  class and status (regular / cancelled / substituted / exam) — cancellations and
  substitutions are the single most useful piece of information for a parent.
- Every response carries the profile name it was produced for, so answers spanning several
  children cannot be mixed up.

### 2.3 Error handling

| ID | Requirement |
|----|-------------|
| FR-06 | Authentication failures, unreachable servers and unsupported features produce explicit, actionable error messages naming the profile. |
| FR-07 | A failure for one profile never affects other profiles. |
| FR-10 | A tool the profile's account is not permitted to use returns an explicit "not available for this profile" result, not an error (ADR-010). |
| FR-08 | Credentials, session cookies and tokens never appear in tool output or in log messages. |

---

## 3. Non-functional requirements

| ID | Requirement |
|----|-------------|
| NFR-01 | Implementation language: Python (>= 3.13, as pinned in `.python-version`). |
| NFR-02 | Dependency and environment management with `uv`. |
| NFR-03 | Code, identifiers, docstrings and documentation in English. |
| NFR-04 | Runs in a container. |
| NFR-05 | Container images must be produced for `linux/arm64` (Raspberry Pi target) from a Windows `amd64` development machine. |
| NFR-06 | A PowerShell build script for Windows and a shell deployment script for Linux. |
| NFR-07 | The configuration file with credentials is never baked into the image; it is mounted or injected at runtime. |
| NFR-08 | Startup validates the configuration and fails fast with a readable message. |

---

## 4. Technology decisions

### ADR-001: FastMCP as the MCP framework

**Decision:** Use `fastmcp` as the server framework. The pin is `fastmcp>=2.0`, which
currently resolves to **4.0.3** — the interview predates that release, so "FastMCP 2.x"
in the original decision simply meant "the `fastmcp` package".

**Rationale:** Explicit preference of the owner. It also fits the requirements well:
decorator-based tool definitions, automatic JSON schema generation from Python type hints
and Pydantic models, and built-in support for both `stdio` and streamable HTTP transports
— the latter is required for the containerised deployment (ADR-006). Its `Client` also
speaks to a server object in memory, which is what makes the tools testable without a
subprocess (section 9).

### ADR-002: Own async HTTP client instead of `python-webuntis`

**Decision:** Implement the WebUntis access layer as our own `httpx`-based **async** client
behind an internal `UntisClient` abstraction. `python-webuntis` is used as a *reference* for
JSON-RPC method names and object shapes, not as a runtime dependency.

**Rationale.** `python-webuntis` was evaluated as the obvious candidate and rejected on
three independent grounds:

*Coverage.* Its `Session` class covers `timetable`, `my_timetable`,
`timetable_with_absences`, `exams`, `holidays`, `substitutions`, `class_reg_events` and
master data. That maps well onto tools 3 and 4 and partly onto tool 2, but there is **no**
API for messages / messages-of-the-day (tool 5), **no** API for the "my appointments" view
(tool 1 could at best be approximated by `exams()`), and absences are reachable only
indirectly. Three of the five data areas need our own HTTP calls in any case.

*Sync/async mismatch.* `python-webuntis` is built on `requests` and is synchronous
throughout. FastMCP is async-first (`httpx`/`anyio`), so every library call would have to be
wrapped in `asyncio.to_thread` — a thread hop, a second HTTP stack in the image, and, more
importantly, a **second cookie jar**. The session cookie obtained at login has to be shared
between the JSON-RPC calls and the REST calls (section 5.1); two clients with separate
cookie stores make that sharing awkward and push towards logging in twice per profile.

*Extending the library was considered and rejected.* The missing features could in principle
be added to `python-webuntis` (fork, or subclass `Session` with `messages()`, `absences()`,
`calendar_entries()`). Against it: the additions are REST- and JWT-based and do not fit its
JSON-RPC `Result`/`ListResult` object model; two of the five areas need a bearer token and a
`tenant-id` header that its session concept knows nothing about; the sync/async problem
above would remain; and we would carry a fork of a third-party library for what is a few
hundred lines of `httpx` in our own code. The library keeps its value as documentation of
the JSON-RPC surface and of the quirks in the wire formats (section 5.3).

**Consequence:** One uniform async client covers all five data areas with a single session,
a single cookie jar and a single retry path. `UntisClient` stays the load-bearing
abstraction: which endpoint serves a given tool — and whether a JSON-RPC fallback is used
for the timetable — remains an implementation detail behind it, so a WebUntis-side change
touches one layer only.

**Risk:** All REST endpoints involved are undocumented and reverse-engineered. The spike of
2026-09-06 verified them against both real tenants — see
[section 5](#5-webuntis-protocol-details) — which settled the feasibility questions but not
the durability ones; [section 10](#10-open-questions-and-risks) carries what remains.

### ADR-003: Configuration format — TOML file with optional environment indirection

**Decision:** Profiles live in a TOML file (default `config/profiles.toml`, overridable via
the `UNTIS_MCP_CONFIG` environment variable). Passwords may be given inline **or** by
naming an environment variable that holds them.

```toml
[[profiles]]
name        = "anna"
description = "Anna, Gymnasium Musterstadt"
server      = "https://ajax.webuntis.com"
school      = "gym-musterstadt"
username    = "anna.mustermann"
password    = "..."                       # or use password_env instead

[[profiles]]
name         = "ben"
description  = "Ben, Realschule Beispieldorf"
server       = "https://herakles.webuntis.com"
school       = "rs-beispieldorf"
username     = "ben.mustermann"
password_env = "UNTIS_BEN_PASSWORD"
```

**Rationale:** TOML is already the project's configuration language (`pyproject.toml`) and
is available in the standard library via `tomllib` — no extra dependency. An array of
tables maps cleanly onto "a list of profiles". The `password_env` indirection keeps the
mounted file free of secrets when the deployment prefers environment injection, without
forcing that on the simple case.

**Alternatives considered:** YAML (extra dependency, whitespace-sensitive), JSON (no
comments), pure environment variables (awkward for a variable number of profiles and hard
to read).

### ADR-004: Pydantic models for configuration and tool results

**Decision:** Parse the configuration into Pydantic models and declare tool return types as
Pydantic models.

**Rationale:** FastMCP derives the tool output schema from the return annotation, which
gives the assistant a typed contract instead of loose dictionaries. The same validation
delivers NFR-08 (fail fast on bad configuration) almost for free, and `SecretStr` for
passwords supports FR-08 (no secrets in logs or reprs).

### ADR-005: Session handling — one cached, lazily created session per profile

**Decision:** Sessions are created lazily on first use per profile, cached in memory,
guarded by a per-profile lock, and transparently re-authenticated when the server reports
an expired session.

**Rationale:** WebUntis logins are comparatively slow and tenants dislike login storms.
Lazy creation means a broken or temporarily unreachable profile neither prevents server
startup nor blocks the other child's data (FR-07).

**Refinement from the protocol research:** sessions expire after roughly **10 minutes of
inactivity**. For this server — which is idle most of the day and then answers a single
question — an expired session is the *normal* case, not an edge case. The cached state per
profile is therefore (session cookie, JWT, `tenant-id`), all of them invalidated together,
and every request path must implement "on 401/403 or a WebUntis auth error: re-authenticate
once, replay the request once, then fail with a profile-tagged error" (FR-06).

### ADR-006: Transport — streamable HTTP in the container, stdio for local development

**Decision:** The container runs FastMCP's streamable HTTP transport on a configurable port
(default 8000). Local development may run the same server over stdio.

**Rationale:** The server runs on a Raspberry Pi while the MCP client runs elsewhere on the
LAN; stdio cannot cross that boundary. Since the server holds credentials for both schools
and performs no authorization of its own, exposure must be constrained by deployment: bind
to the LAN/VPN interface only, do not port-forward from the internet, and optionally
require a static bearer token via `UNTIS_MCP_AUTH_TOKEN`.

### ADR-007: Read-only scope

**Decision:** No tool mutates WebUntis state.

**Rationale:** The purpose is information retrieval. Write operations against a school
platform (excusing an absence, replying to a message) carry real-world consequences and are
deliberately left to the human in the web interface.

### ADR-008: Container image — multi-stage build on `python:3.13-slim`, multi-arch via buildx

**Decision:** Multi-stage Dockerfile: a builder stage installs dependencies with `uv` into a
virtual environment from `uv.lock`; a slim runtime stage copies that environment plus the
application and runs as a non-root user. Images are built for `linux/arm64` (and optionally
`linux/amd64`) with `docker buildx`.

**Rationale:** `uv sync --frozen --no-dev` gives reproducible installs from the existing
lock file. The multi-stage split keeps `uv` and build tooling out of the runtime image,
which matters on a Raspberry Pi. All dependencies are expected to be pure Python, so
cross-building for arm64 under QEMU emulation should be fast enough and no cross-compilation
toolchain is needed.

### ADR-009: The v1 REST timetable view is the primary data source

**Decision:** Serve tools 3 and 4, and the event half of tool 1, from
`GET /WebUntis/api/rest/view/v1/timetable/entries`. Keep `weekly/data` as a fallback and as
the source of the class id, and JSON-RPC `getTimetable` as a cross-check only.

**Rationale:** The spike (section 5.2) found this endpoint delivers strictly more, in a
strictly friendlier shape, than the two sources the design originally assumed:

- ISO timestamps per entry, so neither the `Hmm` decoding nor a time-grid lookup is needed;
- a first-class `status` (`REGULAR` / `CANCELLED` / `CHANGED` / `ADDITIONAL`) plus
  `statusDetail`, instead of the `cellState` / `code` split;
- `current` / `removed` pairs per position, so a substitution carries what it replaced —
  the difference between "maths with Mr X" and "maths with Mr X **instead of** Ms Y";
- school events in the same feed as `type: EVENT`, which is what makes tool 1 feasible
  at all after `/api/exams` turned out to be forbidden.

**Consequence:** The mapping layer's primary input is this payload; `weekly/data` and
JSON-RPC mappers exist for fallback and are exercised by the same domain model. The
JSON-RPC path must clamp its range to the current school year (error `-8507`), which means
the client needs `getCurrentSchoolyear` before it can use that fallback at all.

**Risk:** It is the newest of the three endpoints and the least represented in community
documentation, so it is also the likeliest to change. The fallbacks are the mitigation, and
ADR-002's single client layer is where that choice is made.

### ADR-010: Capabilities are discovered per profile, not assumed

**Decision:** At login, read `permissions` and `user.permissions.views` from `app/data` and
derive a capability set per profile. A tool whose capability is missing returns an explicit
"not available for this profile" result naming the capability, rather than attempting the
call and surfacing an HTTP 403.

**Rationale:** The two tenants are not equivalent (section 5.4). One grants `MESSAGES`, the
other does not — so `get_messages` can never work for one of the two children, and a 403
there is the correct answer rather than a fault to be retried or reported as an outage.
Deciding this from the account's own permission list is more honest than pattern-matching
error codes, and it lets `list_profiles` (FR-05) tell the assistant up front which tools are
worth calling for which child, instead of the model discovering it by failure.

**Consequence:** `list_profiles` reports capabilities alongside names and descriptions
(FR-09). Capability data is part of the cached session state of ADR-005 and is refreshed
with it.

---

## 5. WebUntis protocol details

Everything in this section was **verified on 2026-09-06 against both real school tenants**
with the two parent logins, by the protocol spike (`scripts/probe_endpoints.py`, step 0 of
section 11). Each endpoint is marked *verified* (works on both tenants), *partly verified*
(works on one, refused on the other) or *unavailable*. The two tenants are referred to as
**A** and **B**; which profile is which lives in the local configuration, not in this
document.

The endpoints remain undocumented by Untis, so the defensive posture stands: treat every
field as optional, parse defensively, and let a broken endpoint degrade one tool for one
profile rather than crash the server (FR-06, FR-07).

### 5.1 Authentication and account discovery

1. `POST /WebUntis/jsonrpc.do?school={school}` with `authenticate` (`user`, `password`,
   `client`) → `sessionId`, `personType`, `personId`, `klasseId`.
   **`personId` identifies the parent, not the child** — `personType` is 12
   (`LEGAL_GUARDIAN`) and `klasseId` is 0. Passing it to an element-scoped endpoint yields
   HTTP 403 `{"errorCode":"FORBIDDEN","errorMessage":"no right"}`, and JSON-RPC rejects the
   person type outright with `-7001 invalid elementType: 12`.
2. Every subsequent request carries
   `Cookie: JSESSIONID={sessionId}; schoolname=_{base64(school)}` — the school name
   base64-encoded with a leading underscore.
3. `GET /WebUntis/api/token/new` returns a JWT as a **plain string**, not JSON →
   `Authorization: Bearer {jwt}`.
4. `GET /WebUntis/api/rest/view/v1/app/data` (JWT required) is the **account discovery**
   call, and the single most important request in the whole protocol. It yields:
   - `tenant.id` → the value for the `tenant-id` header. There is no `tenantId` key
     anywhere in the payload; searching for one finds nothing.
   - `user.students[]` → the children this login may read. Their `id` is the element id
     that every data endpoint expects.
   - `user.roles` → `["LEGAL_GUARDIAN"]`.
   - `permissions[]` and `user.permissions.views[]` → what this account may actually do
     (section 5.4).

   Authenticating a profile therefore means resolving it to a triple of **tenant id,
   student id and capabilities**, none of which the login response itself contains.
5. The **class id** is not obtainable from the login either (`klasseId` is 0). It appears
   only inside a timetable payload, as the element of type 1. `/api/exams` demands it — and
   refuses the request anyway (section 5.2).
6. Sessions expire after roughly 10 minutes of inactivity (ADR-005).
7. The two tenants are fully independent: two client instances, two cookie jars, two JWTs,
   two tenant ids, two student ids, two capability sets.

### 5.2 Endpoint map

| Tool | Endpoint and required parameters | Auth | Status |
|------|----------------------------------|------|--------|
| 3, 4, 1 | `GET /WebUntis/api/rest/view/v1/timetable/entries?start={ISO}&end={ISO}&format=1&resourceType=STUDENT&resources={studentId}` | JWT + `tenant-id` | **verified** — the richest source by a wide margin, see below |
| 3, 4 | `GET /WebUntis/api/public/timetable/weekly/data?elementType=5&elementId={studentId}&date={ISO Monday}&formatId=1` | cookie | verified — also the only place the class id appears |
| 3, 4 | JSON-RPC `getTimetable` `{id: studentId, type: 5, startDate, endDate}` | cookie | verified — **but the range must lie inside one school year**, else `-8507` |
| 2 | `GET /WebUntis/api/classreg/absences/students?startDate&endDate&studentId&excuseStatusId=-1` | cookie | **verified** — returns reason, excuse status and the excuse record |
| 5 | `GET /WebUntis/api/rest/view/v1/messages` | JWT + `tenant-id` | **partly verified** — works on A, HTTP 403 on B (no `MESSAGES` permission) |
| 5 | `GET /WebUntis/api/public/news/newsWidgetData?date={yyyyMMdd}` | cookie | verified (empty on both) |
| 1 | `GET /WebUntis/api/rest/view/v2/calendar-entry/detail?elementId={studentId}&elementType=5&startDateTime={ISO+time}&endDateTime={ISO+time}` | JWT + `tenant-id` | reachable on both — but `{"calendarEntries": []}` for the **entire school year** |
| 1 | `GET /WebUntis/api/exams?startDate&endDate&klasseId&withGrades` | cookie | **unavailable** — HTTP 403 on both, even with a real class id |
| — | JSON-RPC `getCurrentSchoolyear`, `getTimegridUnits`, `getHolidays` | cookie | verified — school year bounds, period times, holidays |

Two findings reshape the design.

**`view/v1/timetable/entries` replaces `weekly/data` as the primary timetable source.** It
returns `days[]`, each with a `status` and `gridEntries[]`, and every entry carries ISO
timestamps (`duration.start` / `duration.end`), a `status`
(`REGULAR` / `CANCELLED` / `CHANGED` / `ADDITIONAL`), a `statusDetail` (e.g. `MOVED`), and
`position1..7` slots holding teacher, subject and room — each as a `current` / `removed`
pair, so a substitution states both what happens and what it replaced. No time-grid lookup
and no `Hmm` decoding are needed. Over a five-week window it reported 31 cancellations and
18 changes for A, and 29 changes for B: precisely the information these tools exist to
surface.

**"Meine Termine" comes from that same feed, not from the calendar endpoint.** School events
appear as `gridEntries` with `type: EVENT` (4 for A, 11 for B within five weeks, e.g. a
`lessonInfo` of "Kennenlernwoche"), whereas `calendar-entry/detail` — although reachable and
correctly parameterised — stays empty across the entire school year for both children. Tool
1 is therefore built on `type == EVENT` from the timetable feed, keeping the calendar
endpoint as a secondary source that may fill later in the year. Exams cannot contribute at
all: `/api/exams` is refused for both parent accounts.

### 5.3 Wire formats

Four date encodings coexist, and which one applies is a property of the endpoint:

| Format | Where |
|--------|-------|
| `yyyyMMdd`, as a **number or a string** | JSON-RPC, `/api/classreg/*`, `/api/exams`, `newsWidgetData` |
| `yyyy-MM-dd` | `weekly/data` `date`, `timetable/entries` `start` and `end` |
| `yyyy-MM-ddTHH:mm` | `calendar-entry/detail` `startDateTime` / `endDateTime`, and every `duration` in `timetable/entries` |
| epoch milliseconds | `createDate`, `lastUpdate`, `lastImportTimestamp` |

- **Times:** `Hmm` / `HHmm` without zero padding (`800` = 08:00, `1600` = 16:00) in absences
  and JSON-RPC. The v1 timetable view has no such field at all — it uses full timestamps.
- **Element types** 1 = class, 2 = teacher, 3 = subject, 4 = room, 5 = student. Their
  encoding differs per endpoint: `elementType=5` as a number in `weekly/data` **and** in
  `calendar-entry/detail` (which rejects `"STUDENT"` with "no valid value for the expected
  type: class java.lang.Long"), while `timetable/entries` wants the enum *name* in
  `resourceType=STUDENT`.
- **Three status vocabularies** have to be normalised into the single domain status of
  section 2.2: `status` + `statusDetail` (v1 view), `cellState` (`weekly/data`), and `code`
  (`cancelled` / `irregular`, JSON-RPC).

### 5.4 Per-profile capabilities

`app/data` states what an account may do, and the two tenants genuinely differ. This is open
question 3, answered concretely:

| Capability | A | B | Consequence |
|------------|---|---|-------------|
| `MESSAGES:R:0:ALL`, view `MESSAGE_CENTER` | yes | **no** | `get_messages` is permanently unavailable on B — its 403 is correct, not a defect |
| `STUDENTABSENCES` | yes | yes | tool 2 works on both |
| `TT_VIEW:R:5:ASSIGNED` | yes | yes | tools 3 and 4 work on both |
| `CLASSEVENT:R:0:ASSIGNED` | yes | no | school events; both tenants nevertheless deliver `EVENT` entries in the timetable feed |
| `OFFICEHOURS:R:0:ALL`, `STUDENT_CONTACT_HOURS` | yes | yes | office hours and parent-teacher slots — an unexplored second source for tool 1 |
| `PERFORMANCEASSESSMENT`, `STUDENT_GRADES` | no | yes | grades; out of scope (section 1.2) |

The consequence is architectural rather than incidental: **the server must treat features as
per-profile capabilities** instead of assuming a uniform WebUntis (ADR-010).

---

## 6. Architecture

### 6.1 Layers

```
              MCP client (AI assistant)
                        |  MCP over streamable HTTP (stdio in dev)
   +--------------------v-------------------------------+
   |  Tool layer         (untis_mcp/tools.py)            |  FastMCP tool definitions,
   |                                                     |  argument validation, date
   |                                                     |  defaulting, profile lookup
   +--------------------+--------------------------------+
   |  Domain models      (untis_mcp/models.py)           |  Lesson, TimetableDay,
   |                                                     |  Absence, Message, Appointment
   +--------------------+--------------------------------+
   |  Client layer       (untis_mcp/untis/)              |  UntisClient protocol,
   |                                                     |  session cache, authentication,
   |                                                     |  endpoints, payload mapping
   +--------------------+--------------------------------+
   |  Configuration      (untis_mcp/config.py)           |  TOML loading, profile
   |                                                     |  registry, secret handling
   +--------------------+--------------------------------+
                        |  HTTPS
            WebUntis tenant A     WebUntis tenant B
```

The key structural property: **the tool layer knows nothing about WebUntis**. It resolves a
profile name to a client and asks that client for domain objects. Which HTTP or JSON-RPC
call produces those objects lives entirely in the client layer, so a WebUntis-side change
touches one layer only.

### 6.2 Proposed module layout

```
untis_mcp/
  __init__.py
  __main__.py            # entry point: transport selection, server start
  server.py              # FastMCP instance, lifespan, tool registration
  config.py              # Config / ProfileConfig models, TOML loading, validation
  models.py              # domain models returned by the tools
  tools.py               # the six tools
  errors.py              # ProfileNotFound, UntisAuthError, UntisUnavailable, ...
  untis/
    __init__.py
    client.py            # UntisClient protocol + per-profile client registry
    auth.py              # login flow, session cookie / JWT / tenant-id, expiry + retry
    discovery.py         # app/data -> tenant id, student id, capabilities (ADR-010)
    rest.py              # async REST calls against the WebUntis web API (section 5.2)
    rpc.py               # JSON-RPC calls: authenticate, getTimetable fallback
    mapping.py           # WebUntis payloads -> domain models (date/time/status decoding)
documents/
  requirements-and-design.md
config/
  profiles.example.toml
scripts/
  probe_endpoints.py     # one-off protocol spike, dumps raw payloads as test fixtures
  build.ps1              # Windows build (multi-arch via buildx)
  deploy.sh              # Linux / Raspberry Pi deployment
Dockerfile
compose.yaml
tests/
```

### 6.3 Request flow

1. The assistant calls e.g. `get_timetable_day(profile="anna", date="2026-09-07")`.
2. The tool layer validates the arguments and resolves `"anna"` against the profile
   registry; an unknown name raises `ProfileNotFound` listing the valid names (FR-04).
3. The client registry returns the cached `UntisClient` for that profile, authenticating
   lazily on first use (ADR-005).
4. The client fetches the raw data (section 5.2), retrying the request once after
   re-authentication if the session has expired — the common case given the ~10 minute
   inactivity timeout.
5. The mapping layer converts the payload into domain models.
6. The tool returns a structured result tagged with the profile name.

### 6.4 Cross-cutting concerns

- **Logging** goes to stderr (required for stdio transport correctness) at INFO by default,
  configurable via `UNTIS_MCP_LOG_LEVEL`. Credentials, cookies and tokens are never logged
  (FR-08).
- **Time zone:** WebUntis returns local school time. All timestamps are interpreted as
  `Europe/Berlin` unless a profile overrides it; the container sets `TZ` accordingly.
- **Concurrency:** tools are async; per-profile locks prevent concurrent duplicate logins.

---

## 7. Deployment

### 7.1 Container

- Base image `python:3.13-slim`, multi-stage, non-root runtime user.
- Dependencies installed from `uv.lock` with `uv sync --frozen --no-dev`.
- Configuration is **mounted**, never baked in: `/app/config/profiles.toml` as a read-only
  bind mount, path overridable via `UNTIS_MCP_CONFIG`.
- Exposes the HTTP transport port (default 8000).
- `compose.yaml` for the Raspberry Pi: image reference, config mount, port binding
  restricted to the LAN interface, `restart: unless-stopped`, `TZ`, and optional secret
  environment variables for the `password_env` indirection.

### 7.2 Build and deploy workflow (NFR-05, NFR-06)

Development happens on Windows/amd64; the runtime target is a Raspberry Pi on ARM. Both
architectures are covered by a single `docker buildx` build:

- `scripts/build.ps1` (PowerShell, Windows): ensures a `buildx` builder exists, enables QEMU
  emulation for arm64, builds `linux/arm64` (optionally also `linux/amd64` for local
  testing), and either pushes to a registry or exports an image tarball. Parameters for tag,
  platform list and output mode.
- `scripts/deploy.sh` (Bash, Raspberry Pi): pulls the image or loads the transferred
  tarball, verifies that the configuration file is present, and restarts the compose stack.

Two transport paths between the machines, both supported by the scripts:

1. **Registry** (preferred when one is available): `buildx --push`, then
   `docker compose pull && docker compose up -d` on the Pi.
2. **Tarball** (no registry needed): `buildx --output type=docker,dest=untis-mcp-arm64.tar`
   on Windows, copy with `scp`, `docker load` on the Pi.

Because all dependencies are expected to be pure Python, cross-building under emulation is
viable. If a dependency ever requires native compilation and emulated builds become too
slow, the fallback is to build natively on the Pi — which is why `deploy.sh` is a separate,
self-contained script rather than a step inside the PowerShell build.

---

## 8. Security considerations

- The configuration file holds plaintext credentials for two school accounts. It must be
  excluded from version control (`.gitignore`), mounted read-only, and permission-restricted
  on the host.
- The server performs no authorization: any client that can reach the HTTP port can read
  both children's data. Mitigations: bind to a private interface, never port-forward, and
  optionally require a bearer token (ADR-006).
- Tool output is passed to an AI assistant and therefore potentially to a third-party model
  provider. This is accepted and is the explicit purpose of the project, and it is the
  reason the scope stays read-only and minimal.
- Message content originates from third parties (schools, teachers) and must be treated as
  untrusted data by the consuming assistant, never as instructions.

---

## 9. Testing strategy

- Unit tests for configuration loading and validation, including the `password_env`
  indirection and the unknown-profile error.
- Unit tests for the mapping layer against recorded, anonymised WebUntis payload fixtures
  captured by `scripts/probe_endpoints.py` from **both** tenants (they exist as of
  2026-09-06 and still need anonymising before they enter the repository) — this is where most of the
  fragility lives, and having two tenants in the fixture set is what protects the domain
  model against per-school payload differences.
- Explicit mapping tests for the wire-format quirks of section 5.3: numeric vs. string
  dates, unpadded times (`800`), the four coexisting date encodings, and the three competing
  status vocabularies (`status`/`statusDetail`, `cellState`, `code`).
- A capability test per tool: with the capability absent, the tool answers "not available"
  and makes no HTTP call at all (FR-10).
- A test that an unexpected or missing field degrades into a partial result rather than an
  exception, since the endpoints can change without notice.
- Tool-level tests against a fake `UntisClient`, verifying date defaulting, week resolution
  and profile tagging.
- No test hits a real school tenant in CI.

---

## 10. Open questions and risks

The protocol spike of 2026-09-06 closed the feasibility questions. What it answered:

| Former question | Answer |
|-----------------|--------|
| What does "Meine Termine" contain, and is `calendar-entry/detail` reachable? | The endpoint is reachable on both tenants with `elementId` + `elementType=5` + `startDateTime`/`endDateTime`, but returns nothing all school year. Events instead arrive as `type: EVENT` in the timetable feed (ADR-009). `/api/exams` is forbidden for both parent accounts. |
| Which endpoint serves messages, and does it need a JWT? | `/api/rest/view/v1/messages` with JWT **and** `tenant-id`. It works on tenant A and is permanently forbidden on tenant B (section 5.4). |
| Are absences readable directly by a parent? | Yes — `/api/classreg/absences/students` with the *student* id, including reason, excuse status and excuse record. |
| Do both schools behave the same? | No, and the difference is a permission set, not a version quirk. Hence ADR-010. |
| Is a `tenant-id` obtainable? | Yes, from `app/data` as `tenant.id` — there is no `tenantId` key. |

What remains open:

| # | Item | Impact |
|---|------|--------|
| 1 | `calendar-entry/detail` is empty everywhere so far. Whether it fills for parent-teacher days later in the year is unknown. | Re-probe before assuming tool 1 is complete; `OFFICEHOURS` / `STUDENT_CONTACT_HOURS` (section 5.4) is the unexplored second candidate. |
| 2 | Whether `type: EVENT` is the only event flavour, and which `statusDetail` values exist beyond `MOVED`. | Completeness of the status mapping; mitigated by mapping unknown values to a passthrough rather than failing. |
| 3 | `view/v1/timetable/entries` is the newest and least documented endpoint in use. | Highest exposure to a silent WebUntis change; mitigated by the fallbacks of ADR-009. |
| 4 | The endpoints remain unversioned with no changelog. | Long-term maintenance; the fixtures captured by the spike make a regression detectable. |
| 5 | Is a container registry available, or is the tarball transfer the primary path? | Shape of the deployment scripts. |

---

## 11. Proposed implementation order

0. **Protocol spike** — done, 2026-09-06 (`scripts/probe_endpoints.py`). Verified the
   endpoint map, produced the payloads in `probe-output/`, and rewrote sections 5 and 10.
1. Configuration layer and `list_profiles` — done, 2026-09-06. TOML loading with
   `SecretStr` and `password_env` resolution at startup, case-insensitive profile lookup,
   the FastMCP server, and both transports verified against a real MCP client.
2. Client layer: login, `app/data` discovery (tenant id, student id, capabilities), session
   cache, re-authentication and single retry (sections 5.1, ADR-005, ADR-010).
3. `get_timetable_day` and `get_timetable_week` on `view/v1/timetable/entries` (ADR-009),
   with the mapping tests built on the anonymised spike payloads.
4. `get_absences` — the payload is already understood and both tenants serve it.
5. `get_appointments` from `type: EVENT`, with the calendar endpoint as a secondary source.
6. `get_messages`, including the capability check that makes it a clean "not available" on
   the tenant that forbids it.
7. Dockerfile, `compose.yaml`, `build.ps1`, `deploy.sh`.

The order changed from the original: messages moved to the end because their availability is
now a known per-profile fact rather than an open question, and appointments moved forward
because ADR-009 makes them a filter over data the timetable tools already fetch.

---

## 12. Sources

- Protocol spike output — the primary source for section 5:
  `scripts/probe_endpoints.py`, payloads under `probe-output/` (git-ignored).
- WebUntis REST/JSON-RPC protocol documentation (community):
  <https://github.com/SafeMemoryZone/webuntis-api-docs/blob/master/WEBUNTIS_API.md>
- Home Assistant integration using the REST API, tested with a parent account; source of the
  `tenant-id` requirement: <https://github.com/Naumsede/webuntis-home-assistant>
- `python-webuntis` — reference for JSON-RPC methods and object models (ADR-002):
  <https://github.com/python-webuntis/python-webuntis>
- JavaScript reference implementation — field names for messages, absences and exams:
  <https://github.com/SchoolUtils/WebUntis>
- Official Untis API documentation for integration partners (not usable for this use case,
  see section 5):
  <https://help.untis.at/hc/de/articles/4886785534354-API-documentation-for-integration-partners>
