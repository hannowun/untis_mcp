"""WebUntis access: authentication, capability discovery and the per-profile client.

Everything that knows the WebUntis protocol lives here (section 6.1); the tool layer above
sees only `UntisClient` and domain objects.
"""

from untis_mcp.untis.auth import Session, login
from untis_mcp.untis.capabilities import TOOL_REQUIREMENTS, Capabilities, Requirement
from untis_mcp.untis.client import ClientRegistry, UntisClient

__all__ = [
    "TOOL_REQUIREMENTS",
    "Capabilities",
    "ClientRegistry",
    "Requirement",
    "Session",
    "UntisClient",
    "login",
]
