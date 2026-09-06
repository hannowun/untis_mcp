"""What an account is allowed to do, derived from `app/data` (ADR-010).

WebUntis states permissions in two independent lists, and the spike found both matter:

* ``permissions`` — strings shaped ``NAME:RIGHTS:ELEMENTTYPE:SCOPE``, e.g.
  ``MESSAGES:R:0:ALL`` or ``TT_VIEW:R:5:ASSIGNED``. Only the leading name is matched here;
  the rights and scope differ between tenants for features both of them grant.
* ``user.permissions.views`` — the screens the web client would show, e.g.
  ``STUDENTABSENCES``. Some features appear only here.

A tool is available when *any* of its requirements is present, because the two tenants
express the same capability differently (section 5.4).
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Requirement:
    """What one tool needs. Empty means the tool needs no special permission."""

    permissions: frozenset[str] = frozenset()
    views: frozenset[str] = frozenset()

    @property
    def description(self) -> str:
        """How the requirement is named in an error message the assistant will read."""
        names = sorted(self.permissions | self.views)
        return ", ".join(names)


# The mapping of tools onto WebUntis permissions. Verified against both tenants on
# 2026-09-06; see section 5.4 of the design document for what each of them grants.
TOOL_REQUIREMENTS: dict[str, Requirement] = {
    "get_timetable_day": Requirement(
        permissions=frozenset({"TT_VIEW"}),
        views=frozenset({"TIMETABLE_NEW_STUDENTS_MY", "PERMISSION_STUDENT_TT_OWN"}),
    ),
    "get_timetable_week": Requirement(
        permissions=frozenset({"TT_VIEW"}),
        views=frozenset({"TIMETABLE_NEW_STUDENTS_MY", "PERMISSION_STUDENT_TT_OWN"}),
    ),
    # Appointments are EVENT entries of the same timetable feed (ADR-009), so they stand or
    # fall with the timetable permission rather than with CLASSEVENT, which only one tenant
    # grants although both deliver the events.
    "get_appointments": Requirement(
        permissions=frozenset({"TT_VIEW"}),
        views=frozenset({"TIMETABLE_NEW_STUDENTS_MY", "PERMISSION_STUDENT_TT_OWN"}),
    ),
    "get_absences": Requirement(
        permissions=frozenset({"STUDABS_REPORT"}),
        views=frozenset({"STUDENTABSENCES"}),
    ),
    "get_messages": Requirement(
        permissions=frozenset({"MESSAGES"}),
        views=frozenset({"MESSAGE_CENTER", "MESSAGE_CENTER_2021"}),
    ),
    # Messages of the day come from a public endpoint that answered on both tenants without
    # any matching permission entry, so it is deliberately ungated.
    "get_messages_of_day": Requirement(),
}


@dataclass(frozen=True)
class Capabilities:
    """The permission sets of one account, and the questions worth asking of them."""

    permissions: frozenset[str] = field(default_factory=frozenset)
    views: frozenset[str] = field(default_factory=frozenset)

    @classmethod
    def from_app_data(cls, app_data: object) -> Capabilities:
        """Read both lists out of an `app/data` payload, tolerating a changed shape."""
        data = app_data if isinstance(app_data, dict) else {}
        raw_permissions = data.get("permissions")
        permissions = {
            str(item).split(":", 1)[0] for item in (raw_permissions or []) if isinstance(item, str)
        }
        user = data.get("user") if isinstance(data.get("user"), dict) else {}
        user_permissions = user.get("permissions") if isinstance(user, dict) else {}
        raw_views = user_permissions.get("views") if isinstance(user_permissions, dict) else []
        views = {str(item) for item in (raw_views or [])}
        return cls(permissions=frozenset(permissions), views=frozenset(views))

    def allows(self, tool: str) -> bool:
        """Whether this account may use `tool`. Unknown tools are allowed."""
        requirement = TOOL_REQUIREMENTS.get(tool)
        if requirement is None:
            return True
        if not requirement.permissions and not requirement.views:
            return True
        return bool(self.permissions & requirement.permissions or self.views & requirement.views)

    def missing_for(self, tool: str) -> list[str]:
        """The requirement names to quote when refusing, in a stable order."""
        requirement = TOOL_REQUIREMENTS.get(tool)
        return sorted(requirement.permissions | requirement.views) if requirement else []

    @property
    def supported_tools(self) -> list[str]:
        """Every tool this account may use, for `list_profiles` (FR-09)."""
        return sorted(tool for tool in TOOL_REQUIREMENTS if self.allows(tool))
