"""The MCP tools.

The tool layer knows nothing about WebUntis (section 6.1): it validates arguments, resolves
a profile name, and asks the client layer. The five data tools follow in steps 3 to 6 of
section 11; what exists here is profile discovery and the shared plumbing they will use.
"""

from __future__ import annotations

import logging

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError

from untis_mcp.config import Config, ProfileConfig
from untis_mcp.errors import ProfileNotFound, UntisMcpError
from untis_mcp.models import ProfileSummary
from untis_mcp.untis.client import ClientRegistry, UntisClient

logger = logging.getLogger(__name__)


def resolve_profile(config: Config, name: str) -> ProfileConfig:
    """Resolve a `profile` argument, or fail with a message that lists the valid names.

    `ToolError` is what reaches the assistant, so the message has to be enough for it to
    retry correctly without a second round trip (FR-04).
    """
    try:
        return config.get(name)
    except ProfileNotFound as exc:
        raise ToolError(str(exc)) from exc


async def client_for(config: Config, registry: ClientRegistry, name: str, tool: str) -> UntisClient:
    """Resolve a profile, authenticate it, and check that it may use `tool`.

    Every failure here is turned into a `ToolError`, because these are answers the assistant
    needs to read and act on — a wrong profile name, an unreachable school, or a feature the
    account is not permitted to use (FR-06, ADR-010).
    """
    profile = resolve_profile(config, name)
    client = registry.for_profile(profile)
    try:
        await client.session()
        client.require(tool)
    except UntisMcpError as exc:
        raise ToolError(str(exc)) from exc
    return client


def register_tools(mcp: FastMCP, config: Config, registry: ClientRegistry) -> None:
    """Register every tool against this configuration."""

    @mcp.tool
    def list_profiles() -> list[ProfileSummary]:
        """List the configured children and schools this server can answer questions about.

        Call this first when you do not already know which profiles exist. Every other tool
        takes one of these names as its `profile` argument. No credentials are returned.

        This never contacts the schools, so it always answers. `supported_tools` is
        therefore filled in only for profiles already used in this session.
        """
        logger.debug("list_profiles -> %d profile(s)", len(config.profiles))
        summaries = []
        for profile in config.profiles:
            capabilities = registry.cached_capabilities(profile)
            summaries.append(
                ProfileSummary(
                    name=profile.name,
                    description=profile.description,
                    supported_tools=capabilities.supported_tools if capabilities else None,
                )
            )
        return summaries
