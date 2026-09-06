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


class UntisError(UntisMcpError):
    """Something went wrong while talking to a WebUntis tenant, for one profile."""

    def __init__(self, profile: str, message: str) -> None:
        self.profile = profile
        super().__init__(f"profile {profile!r}: {message}")


class UntisAuthError(UntisError):
    """The login itself failed — wrong credentials, or the account is locked out."""


class UntisUnavailable(UntisError):
    """The school's server could not be reached or answered with a server error."""


class StudentAmbiguous(UntisError):
    """The login sees several children and the profile does not say which one (FR-11)."""

    def __init__(self, profile: str, students: Iterable[tuple[int, str]]) -> None:
        self.students = list(students)
        listed = ", ".join(f"{name} (student_id = {sid})" for sid, name in self.students)
        super().__init__(
            profile,
            f"this login can see several children: {listed}. "
            f"Add student_id to the profile in the configuration file to choose one.",
        )


class FeatureNotAvailable(UntisError):
    """The account is not permitted to use this feature — a fact, not a fault (ADR-010)."""

    def __init__(self, profile: str, tool: str, missing: Iterable[str]) -> None:
        self.tool = tool
        self.missing = list(missing)
        listed = " or ".join(self.missing)
        super().__init__(
            profile,
            f"{tool} is not available, because this school's account does not grant "
            f"{listed}. This is a permanent property of the account, not a temporary "
            f"failure — do not retry it.",
        )
