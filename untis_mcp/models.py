"""Domain models returned by the tools (ADR-004).

FastMCP derives each tool's output schema from these, so field descriptions are part of the
contract the assistant sees — they are written for a model to read.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class ProfileSummary(BaseModel):
    """One configured child/school, as reported by `list_profiles`. Never holds secrets."""

    name: str = Field(description="Pass this as the 'profile' argument of every other tool.")
    description: str = Field(
        default="",
        description="Who this profile is for, e.g. which child and which school.",
    )
    supported_tools: list[str] | None = Field(
        default=None,
        description=(
            "Tools this profile's account is permitted to use. Null means it has not been "
            "determined yet, because that requires logging in to the school's server; it "
            "does not mean the tools are unavailable."
        ),
    )
