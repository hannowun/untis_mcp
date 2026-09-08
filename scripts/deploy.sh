#!/usr/bin/env bash
# Deploy untis-mcp on the Raspberry Pi (NFR-06).
#
# Three ways to get an image, in the order they are usually wanted:
#   --tarball FILE   load an image built on the Windows machine and copied across
#   --pull           pull from a registry
#   --build          build here on the Pi, the fallback if emulated cross-builds ever break
#
# Whichever is used, the same two files have to be right before anything starts:
#
#   config/profiles.toml   the profiles and their credentials — a server that came up with
#                          none would look healthy and answer nothing
#   .env                   which interface and port this machine exposes, which image to
#                          run, and the secrets the profiles reference
#
# Both are checked here rather than left to fail inside the container, because a compose
# variable that is merely unset produces an empty string and a container listening
# somewhere nobody expected.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CONFIG="${UNTIS_MCP_CONFIG_FILE:-$REPO_ROOT/config/profiles.toml}"
ENV_FILE="${UNTIS_MCP_ENV_FILE:-$REPO_ROOT/.env}"
MODE=""
TARBALL=""

usage() {
    cat <<EOF
Deploy untis-mcp on this machine.

Usage: $0 [--tarball FILE | --pull | --build] [--no-start]

  --tarball FILE  load an image tarball built elsewhere (scripts/build.ps1)
  --pull          pull the image named by UNTIS_MCP_IMAGE from its registry
  --build         build the image here, then deploy it
  --no-start      stop once the image is in place, do not touch the running stack

With no image source, whatever image is already present locally is used.

Reads $ENV_FILE (copy .env.example) and $CONFIG.
EOF
    exit "${1:-0}"
}

NO_START=0
while [ $# -gt 0 ]; do
    case "$1" in
        --tarball) MODE="tarball"; TARBALL="${2:?--tarball needs a file}"; shift 2 ;;
        --pull) MODE="pull"; shift ;;
        --build) MODE="build"; shift ;;
        --no-start) NO_START=1; shift ;;
        -h|--help) usage 0 ;;
        *) echo "unknown argument: $1" >&2; usage 1 ;;
    esac
done

cd "$REPO_ROOT"

# --- preconditions --------------------------------------------------------------------

if ! command -v docker >/dev/null 2>&1; then
    echo "docker is not installed on this machine." >&2
    exit 1
fi

if ! docker compose version >/dev/null 2>&1; then
    echo "docker compose (v2) is required; 'docker-compose' is not enough." >&2
    exit 1
fi

# compose.yaml uses the long env_file syntax, which arrived in Compose 2.24.
compose_version="$(docker compose version --short 2>/dev/null || echo 0)"
compose_version="${compose_version#v}"
if [ "$(printf '%s\n%s\n' "$compose_version" "2.24.0" | sort -V | head -n1)" != "2.24.0" ]; then
    echo "docker compose 2.24 or newer is required (this is $compose_version)." >&2
    echo "Update the plugin: sudo apt install --only-upgrade docker-compose-plugin" >&2
    exit 1
fi

if [ ! -f "$CONFIG" ]; then
    cat >&2 <<EOF
No configuration at $CONFIG.

Copy config/profiles.example.toml to that path and fill in the real credentials, then run
this again. The file is mounted read-only into the container and is never part of an image.
EOF
    exit 1
fi

if [ ! -f "$ENV_FILE" ]; then
    cat >&2 <<EOF
No environment file at $ENV_FILE.

Copy .env.example to that path and set at least UNTIS_MCP_BIND and UNTIS_MCP_EXPOSED_PORT,
which decide what this machine exposes to the network:

    cp .env.example .env && chmod 600 .env && \$EDITOR .env
EOF
    exit 1
fi

# Both files hold plaintext secrets; make them hard to read by accident.
for secret_file in "$CONFIG" "$ENV_FILE"; do
    permissions="$(stat -c '%a' "$secret_file")"
    if [ "${permissions: -2}" != "00" ]; then
        echo "Tightening permissions on $secret_file (was $permissions, now 600)."
        chmod 600 "$secret_file"
    fi
done

# --- the environment file -------------------------------------------------------------

declare -A ENV_VARS=()

# Read it the way compose does, and no further: KEY=VALUE, last assignment wins, optional
# `export `, surrounding quotes stripped, no shell expansion and nothing executed.
parse_env_file() {
    local line key value
    while IFS= read -r line || [ -n "$line" ]; do
        line="${line%$'\r'}"                       # a file edited on Windows
        line="${line#"${line%%[![:space:]]*}"}"    # leading whitespace
        case "$line" in ''|'#'*) continue ;; esac
        line="${line#export }"
        case "$line" in *=*) ;; *) continue ;; esac
        key="${line%%=*}"
        value="${line#*=}"
        key="${key%"${key##*[![:space:]]}"}"       # trailing whitespace in the key
        case "$value" in
            '"'*'"') value="${value#\"}"; value="${value%\"}" ;;
            "'"*"'") value="${value#\'}"; value="${value%\'}" ;;
        esac
        ENV_VARS["$key"]="$value"
    done < "$ENV_FILE"
}

parse_env_file

missing=()
problems=()
warnings=()

need() {
    local key="$1" purpose="$2"
    if [ -z "${ENV_VARS[$key]:-}" ]; then
        missing+=("$key — $purpose")
    fi
}

need UNTIS_MCP_IMAGE        "the image to run, e.g. untis-mcp:latest"
need UNTIS_MCP_BIND         "the host interface the port is published on"
need UNTIS_MCP_EXPOSED_PORT "the host port MCP clients connect to"
need UNTIS_MCP_PORT         "the port the server listens on inside the container"
need TZ                     "the timezone school days are interpreted in"

if [ ${#missing[@]} -gt 0 ]; then
    echo "These variables are missing or empty in $ENV_FILE:" >&2
    printf '  %s\n' "${missing[@]}" >&2
    echo >&2
    echo ".env.example documents every one of them." >&2
    exit 1
fi

bind="${ENV_VARS[UNTIS_MCP_BIND]}"
exposed_port="${ENV_VARS[UNTIS_MCP_EXPOSED_PORT]}"
container_port="${ENV_VARS[UNTIS_MCP_PORT]}"

for port_var in UNTIS_MCP_EXPOSED_PORT UNTIS_MCP_PORT; do
    port="${ENV_VARS[$port_var]}"
    if ! [[ "$port" =~ ^[0-9]+$ ]] || [ "$port" -lt 1 ] || [ "$port" -gt 65535 ]; then
        problems+=("$port_var is '$port', which is not a port number between 1 and 65535")
    elif [ "$port_var" = "UNTIS_MCP_EXPOSED_PORT" ] && [ "$port" -lt 1024 ]; then
        warnings+=("$port_var is $port; publishing a port below 1024 needs a privileged docker daemon")
    fi
done

# An address this machine does not have is the failure this check exists for: docker reports
# it as "cannot assign requested address" long after everything else looked fine.
if [[ "$bind" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]] && [ "$bind" != "0.0.0.0" ]; then
    local_addresses="$(ip -4 -o addr show 2>/dev/null | awk '{print $4}' | cut -d/ -f1 || true)"
    if [ -n "$local_addresses" ] && ! printf '%s\n' "$local_addresses" | grep -qx "$bind"; then
        problems+=(
            "UNTIS_MCP_BIND is $bind, which is not an address of this machine."
            "  This machine has: $(echo $local_addresses) (or 0.0.0.0 for all of them)"
        )
    fi
fi

# The server authorizes nobody, so anything wider than the loopback wants a token.
case "$bind" in
    127.*|::1|localhost) ;;
    *)
        if [ "$bind" = "0.0.0.0" ]; then
            warnings+=("UNTIS_MCP_BIND is 0.0.0.0 — every interface of this machine, including any that faces a router.")
        fi
        if [ -z "${ENV_VARS[UNTIS_MCP_AUTH_TOKEN]:-}" ]; then
            warnings+=(
                "UNTIS_MCP_BIND is $bind but UNTIS_MCP_AUTH_TOKEN is empty: everything that can"
                "  reach $bind:$exposed_port will be able to read all profiles."
            )
        fi
        ;;
esac

# A profile that names an environment variable for its password needs that variable to
# arrive in the container, and the env file is the only thing that carries it there.
while IFS= read -r password_var; do
    [ -n "$password_var" ] || continue
    if [ -z "${ENV_VARS[$password_var]:-}" ]; then
        problems+=("a profile in $CONFIG has password_env = \"$password_var\", but $password_var is not set in $ENV_FILE")
    fi
done < <(grep -oE 'password_env[[:space:]]*=[[:space:]]*"[^"]+"' "$CONFIG" | sed 's/.*"\(.*\)"/\1/')

if [ ${#problems[@]} -gt 0 ]; then
    echo "Problems in $ENV_FILE:" >&2
    printf '  %s\n' "${problems[@]}" >&2
    exit 1
fi

if [ ${#warnings[@]} -gt 0 ]; then
    printf 'Warning: %s\n' "${warnings[@]}" >&2
    echo >&2
fi

# compose finds .env next to compose.yaml by itself; anywhere else has to be passed on, and
# exported as well so the env_file entry inside compose.yaml resolves to the same file.
DC=(docker compose)
if [ "$ENV_FILE" != "$REPO_ROOT/.env" ]; then
    export UNTIS_MCP_ENV_FILE="$ENV_FILE"
    DC+=(--env-file "$ENV_FILE")
fi

echo "Publishing $bind:$exposed_port -> container port $container_port, image ${ENV_VARS[UNTIS_MCP_IMAGE]}."

# --- get the image --------------------------------------------------------------------

case "$MODE" in
    tarball)
        [ -f "$TARBALL" ] || { echo "no such file: $TARBALL" >&2; exit 1; }
        echo "Loading image from $TARBALL..."
        docker load --input "$TARBALL"
        ;;
    pull)
        echo "Pulling image..."
        "${DC[@]}" pull
        ;;
    build)
        echo "Building on this machine (this takes a while on a Pi)..."
        "${DC[@]}" build
        ;;
    "")
        echo "No image source given; using whatever is already present locally."
        ;;
esac

# --- start ----------------------------------------------------------------------------

if [ "$NO_START" -eq 1 ]; then
    echo "Image ready. Not starting, as requested."
    exit 0
fi

# Something else on the host port leaves compose to fail with a bind error; our own
# container holding it is the normal case on a redeploy and not worth mentioning.
if ! "${DC[@]}" ps -q untis-mcp 2>/dev/null | grep -q .; then
    if command -v ss >/dev/null 2>&1 && ss -ltnH "sport = :$exposed_port" 2>/dev/null | grep -q .; then
        echo "Warning: something is already listening on port $exposed_port." >&2
    fi
fi

echo "Starting the stack..."
"${DC[@]}" up -d

# Give the container a moment to either come up or fall over, then say which happened.
# Health is deliberately not waited for here: on a Pi the server takes a while to import,
# and "running" is the question this script can answer honestly right away.
sleep 5
state="$("${DC[@]}" ps --format '{{.State}}' untis-mcp 2>/dev/null || echo unknown)"
echo "Container state: $state"

if [ "$state" != "running" ]; then
    echo
    echo "It is not running. The last lines of its log:" >&2
    "${DC[@]}" logs --tail 30 untis-mcp >&2 || true
    exit 1
fi

published="$("${DC[@]}" port untis-mcp "$container_port" 2>/dev/null || true)"
echo
echo "untis-mcp is up${published:+ on $published}."
echo "Point the MCP client at http://${published:-$bind:$exposed_port}/mcp"
echo "Logs: ${DC[*]} logs -f untis-mcp"
echo "It reports healthy once it finishes starting: ${DC[*]} ps"
