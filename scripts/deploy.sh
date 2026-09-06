#!/usr/bin/env bash
# Deploy untis-mcp on the Raspberry Pi (NFR-06).
#
# Three ways to get an image, in the order they are usually wanted:
#   --tarball FILE   load an image built on the Windows machine and copied across
#   --pull           pull from a registry
#   --build          build here on the Pi, the fallback if emulated cross-builds ever break
#
# It refuses to start without a configuration file, because a server that came up with no
# profiles would look healthy and answer nothing.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CONFIG="${UNTIS_MCP_CONFIG_FILE:-$REPO_ROOT/config/profiles.toml}"
MODE=""
TARBALL=""

usage() {
    sed -n '2,10p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
    echo
    echo "Usage: $0 [--tarball FILE | --pull | --build] [--no-start]"
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

if [ ! -f "$CONFIG" ]; then
    cat >&2 <<EOF
No configuration at $CONFIG.

Copy config/profiles.example.toml to that path and fill in the real credentials, then run
this again. The file is mounted read-only into the container and is never part of an image.
EOF
    exit 1
fi

# The file holds plaintext school passwords; make that hard to read by accident.
permissions="$(stat -c '%a' "$CONFIG")"
if [ "${permissions: -2}" != "00" ]; then
    echo "Tightening permissions on $CONFIG (was $permissions, now 600)."
    chmod 600 "$CONFIG"
fi

# --- get the image --------------------------------------------------------------------

case "$MODE" in
    tarball)
        [ -f "$TARBALL" ] || { echo "no such file: $TARBALL" >&2; exit 1; }
        echo "Loading image from $TARBALL..."
        docker load --input "$TARBALL"
        ;;
    pull)
        echo "Pulling image..."
        docker compose pull
        ;;
    build)
        echo "Building on this machine (this takes a while on a Pi)..."
        docker compose build
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

echo "Starting the stack..."
docker compose up -d

# Give the container a moment to either come up or fall over, then say which happened.
# Health is deliberately not waited for here: on a Pi the server takes a while to import,
# and "running" is the question this script can answer honestly right away.
sleep 5
state="$(docker compose ps --format '{{.State}}' untis-mcp 2>/dev/null || echo unknown)"
echo "Container state: $state"

if [ "$state" != "running" ]; then
    echo
    echo "It is not running. The last lines of its log:" >&2
    docker compose logs --tail 30 untis-mcp >&2 || true
    exit 1
fi

published="$(docker compose port untis-mcp 8000 2>/dev/null || true)"
echo
echo "untis-mcp is up${published:+ on $published}."
echo "Point the MCP client at http://${published:-127.0.0.1:8000}/mcp"
echo "Logs: docker compose logs -f untis-mcp"
echo "It reports healthy once it finishes starting: docker compose ps"
