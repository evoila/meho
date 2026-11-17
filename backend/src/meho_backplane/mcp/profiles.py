# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tool profiles: a session asks for a smaller tool list (#4172).

A client adds ``?profile=<name>`` to the MCP URL. MEHO then lists only the
tools of that profile, and refuses a call to any other tool. Without a
profile the session sees the full list, as before.

A profile only takes tools away. The role, capability, surface and add-on
gates in :func:`~meho_backplane.mcp.registry.all_tools_for` still run
first, so a profile never shows a tool the session may not use.

A profile is a URL setting, not a request header: every MCP client can set
the server URL, and not every client can set a header.

A built-in profile does not change what the person may do.
``call_operation`` keeps the person's role, the connector's risk level and
the approval rules. The profile only makes the tool list shorter, so a
small model keeps more of its context window for the job.

Profiles with a connector list (#6099). With the
``MCP_PROFILE_CONNECTOR_LISTS`` switch on, an admin can add profiles in
``MCP_CONNECTOR_PROFILES`` (:mod:`meho_backplane.mcp_profile_config`). Such
a profile also limits what the session may do, on top of the person's
rules:

* The tools that take a ``connector_id`` (``call_operation``,
  ``preview_operation``, ``search_operations``, ``list_operation_groups``)
  refuse a connector outside the profile, and ``meho_connector_list``
  shows only the connectors of the profile. The check compares the
  ``connector_id`` argument, so it also covers a raw ``METHOD:/path``
  operation.
* A read-only profile has no MEHO write tools, and the policy gate refuses
  every operation that changes something
  (:func:`profile_denial`).

A login token can carry the profile in the ``mcp_profile`` claim. MEHO then
takes that token only at ``/mcp?profile=<the same name>``; see
:mod:`meho_backplane.auth.jwt`.
"""

from __future__ import annotations

from typing import Final

from meho_backplane.mcp_profile_config import ConnectorProfile
from meho_backplane.settings import get_settings

__all__ = [
    "INVESTIGATE_TOOLS",
    "MEHO_WRITE_TOOLS",
    "OPERATE_TOOLS",
    "TOOL_PROFILES",
    "UnknownToolProfileError",
    "connector_in_families",
    "is_known_profile",
    "parse_tool_profile",
    "profile_admits",
    "profile_admits_connector",
    "profile_connectors",
    "profile_denial",
]

#: Read the systems and what the team knows. No preview, no broadcast.
INVESTIGATE_TOOLS: Final[frozenset[str]] = frozenset(
    {
        "meho_status",
        "meho_connector_list",
        "list_targets",
        "list_operation_groups",
        "search_operations",
        "call_operation",
        "result_query",
        "search_knowledge",
        "search_memory",
    }
)

#: Investigate, plus preview a change and tell the other agents about it.
#: All three broadcast tools are here, because the broadcast part of the
#: session-start text names all three.
OPERATE_TOOLS: Final[frozenset[str]] = INVESTIGATE_TOOLS | frozenset(
    {
        "preview_operation",
        "meho_broadcast_recent",
        "meho_broadcast_announce",
        "meho_broadcast_watch",
    }
)

#: Every profile a client may name, with the tools it keeps.
TOOL_PROFILES: Final[dict[str, frozenset[str]]] = {
    "investigate": INVESTIGATE_TOOLS,
    "operate": OPERATE_TOOLS,
}

#: MEHO's own tools that change something. A read-only profile never keeps
#: them, because they do not check the person's grants.
MEHO_WRITE_TOOLS: Final[frozenset[str]] = frozenset(
    {
        "add_to_memory",
        "add_to_knowledge",
        "meho_broadcast_announce",
        "meho_runbook_start",
        "meho_runbook_next",
        "meho_runbook_abort",
    }
)

#: An error text repeats at most three names from the URL, each cut to
#: this many characters.
_MAX_SHOWN_CHARS: Final[int] = 64


def _configured_profiles() -> dict[str, ConnectorProfile]:
    """Return the profiles with a connector list, or none when the switch is off."""
    settings = get_settings()
    if not settings.mcp_profile_connector_lists:
        return {}
    return settings.mcp_connector_profiles


def _known_profile_names() -> list[str]:
    return sorted({*TOOL_PROFILES, *_configured_profiles()})


def is_known_profile(name: str) -> bool:
    """Return True when *name* is a built-in profile or a configured one."""
    return name in TOOL_PROFILES or name in _configured_profiles()


def _profile_tools(name: str) -> frozenset[str]:
    """Return the tools *name* keeps; none for an unknown name."""
    if name in TOOL_PROFILES:
        return TOOL_PROFILES[name]
    configured = _configured_profiles().get(name)
    if configured is None:
        return frozenset()
    tools = TOOL_PROFILES[configured.tools]
    return tools - MEHO_WRITE_TOOLS if configured.read_only else tools


class UnknownToolProfileError(ValueError):
    """The URL names a profile MEHO does not have, or names more than one."""

    def __init__(self, names: list[str]) -> None:
        self.names = names
        known = ", ".join(_known_profile_names())
        shown = ", ".join(repr(name[:_MAX_SHOWN_CHARS]) for name in names[:3])
        super().__init__(
            f"unknown tool profile {shown}. Name one of: {known}. "
            "Leave out the profile to get the full tool list."
        )


def parse_tool_profile(values: list[str]) -> str | None:
    """Return the profile name from the ``profile`` URL parameter values.

    Names are not case-sensitive. No value, or only blank values, means
    no profile (the full list). An unknown name, or two different names,
    raise :class:`UnknownToolProfileError`: a mistake must not quietly give
    the full list.
    """
    names = sorted({value.strip().lower() for value in values if value.strip()})
    if not names:
        return None
    if len(names) > 1 or not is_known_profile(names[0]):
        raise UnknownToolProfileError(names)
    return names[0]


def profile_admits(profile: str | None, tool_name: str) -> bool:
    """Return True when *profile* keeps *tool_name*.

    No profile keeps every tool. An unknown profile keeps none, so a
    mistake fails closed.
    """
    if profile is None:
        return True
    return tool_name in _profile_tools(profile)


def connector_in_families(connector_id: str, families: tuple[str, ...]) -> bool:
    """Return True when *connector_id* belongs to one of *families*.

    A family name counts for the connector with exactly that id, and for
    every connector whose id starts with the name and a dash. So
    ``kubernetes`` counts for ``kubernetes-1.x`` and ``vmware-rest`` counts
    for ``vmware-rest-9.0``. Case does not matter.
    """
    wanted = connector_id.strip().lower()
    return any(wanted == family or wanted.startswith(f"{family}-") for family in families)


def profile_connectors(profile: str | None) -> tuple[str, ...] | None:
    """Return the connector families of *profile*, or ``None`` for no limit.

    A built-in profile and no profile have no limit. An unknown profile
    gets an empty tuple: it may use no connector.
    """
    if profile is None or profile in TOOL_PROFILES:
        return None
    configured = _configured_profiles().get(profile)
    return () if configured is None else configured.connectors


def profile_admits_connector(profile: str | None, connector_id: object) -> bool:
    """Return True when a session of *profile* may use *connector_id*.

    A value that is not text is refused when the profile has a limit, so a
    wrong type never passes the check.
    """
    families = profile_connectors(profile)
    if families is None:
        return True
    return isinstance(connector_id, str) and connector_in_families(connector_id, families)


def profile_denial(profile: str | None, connector_id: str | None, *, mutating: bool) -> str | None:
    """Return why the policy gate refuses this call for *profile*, or ``None``.

    The profile's rule is added to the person's rules, so the stricter one
    wins. A connector outside the profile is refused. A read-only profile
    refuses every operation that changes something. No profile and the
    built-in profiles refuse nothing here.
    """
    families = profile_connectors(profile)
    if families is None:
        return None
    if connector_id is None or not connector_in_families(connector_id, families):
        return f"the connector is not in your {profile!r} profile"
    configured = _configured_profiles().get(profile or "")
    if mutating and (configured is None or configured.read_only):
        return f"your {profile!r} profile is read-only; this operation changes something"
    return None
