"""Exceptions raised by the server.

Kept deliberately small: the tool layer converts these into FastMCP `ToolError`s, whose
messages reach the assistant, so every message here is written to be read by a model and by
a human operator (FR-04, FR-06).
"""

from __future__ import annotations

from collections.abc import Iterable


class UntisMcpError(Exception):
    """Base class for every error this server raises deliberately."""


class ConfigError(UntisMcpError):
    """The configuration file is missing, unreadable or invalid (NFR-08)."""


class ProfileNotFound(UntisMcpError):
    """A tool was called with a profile name that is not configured (FR-04)."""

    def __init__(self, name: str, available: Iterable[str]) -> None:
        self.name = name
        self.available = list(available)
        listed = ", ".join(repr(item) for item in self.available) or "none configured"
        super().__init__(f"unknown profile {name!r}; configured profiles: {listed}")
