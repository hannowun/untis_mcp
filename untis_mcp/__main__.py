"""Entry point: logging, configuration, transport selection (ADR-006).

Transports differ by deployment: stdio for local development, streamable HTTP in the
container, where the MCP client lives on another machine (section 7.1).
"""

from __future__ import annotations

import logging
import os
import sys

from untis_mcp.config import load_config
from untis_mcp.errors import ConfigError
from untis_mcp.server import create_server

TRANSPORT_ENV = "UNTIS_MCP_TRANSPORT"
HOST_ENV = "UNTIS_MCP_HOST"
PORT_ENV = "UNTIS_MCP_PORT"
TOKEN_ENV = "UNTIS_MCP_AUTH_TOKEN"
LOG_LEVEL_ENV = "UNTIS_MCP_LOG_LEVEL"

logger = logging.getLogger("untis_mcp")


def configure_logging() -> None:
    """Log to stderr — stdout carries the MCP protocol under the stdio transport."""
    logging.basicConfig(
        stream=sys.stderr,
        level=os.environ.get(LOG_LEVEL_ENV, "INFO").upper(),
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
    )


def main() -> int:
    configure_logging()

    try:
        config = load_config()
    except ConfigError as exc:
        # NFR-08: a bad configuration must be legible, not a traceback.
        print(f"untis-mcp: {exc}", file=sys.stderr)
        return 2

    server = create_server(config, auth_token=os.environ.get(TOKEN_ENV))

    transport = os.environ.get(TRANSPORT_ENV, "stdio").strip().lower()
    if transport == "stdio":
        server.run(transport="stdio", show_banner=False)
    elif transport in {"http", "streamable-http"}:
        host = os.environ.get(HOST_ENV, "127.0.0.1")
        port = int(os.environ.get(PORT_ENV, "8000"))
        logger.info("serving MCP over HTTP on %s:%d", host, port)
        server.run(transport="http", host=host, port=port, show_banner=False)
    else:
        print(
            f"untis-mcp: unknown {TRANSPORT_ENV}={transport!r}; use 'stdio' or 'http'",
            file=sys.stderr,
        )
        return 2

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
