"""The MCP tools.

The tool layer knows nothing about WebUntis (section 6.1): it validates arguments, resolves
a profile name, and hands off. Only `list_profiles` exists so far — the data tools arrive
with the client layer in step 2 of section 11.
"""

from __future__ import annotations

import logging

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError

from untis_mcp.config import Config, ProfileConfig
from untis_mcp.errors import ProfileNotFound
from untis_mcp.models import ProfileSummary

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


def register_tools(mcp: FastMCP, config: Config) -> None:
    """Register every tool against this configuration."""

    @mcp.tool
    def list_profiles() -> list[ProfileSummary]:
        """List the configured children and schools this server can answer questions about.

        Call this first when you do not already know which profiles exist. Every other tool
        takes one of these names as its `profile` argument. No credentials are returned.
        """
        logger.debug("list_profiles -> %d profile(s)", len(config.profiles))
        return [
            ProfileSummary(name=profile.name, description=profile.description)
            for profile in config.profiles
        ]
