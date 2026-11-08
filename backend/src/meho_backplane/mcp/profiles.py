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

A profile does not change what the person may do. ``call_operation`` keeps
the person's role, the connector's risk level and the approval rules. The
profile only makes the tool list shorter, so a small model keeps more of
its context window for the job.
"""

from __future__ import annotations

from typing import Final

__all__ = [
    "INVESTIGATE_TOOLS",
    "OPERATE_TOOLS",
    "TOOL_PROFILES",
    "UnknownToolProfileError",
    "parse_tool_profile",
    "profile_admits",
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

#: An error text repeats at most three names from the URL, each cut to
#: this many characters.
_MAX_SHOWN_CHARS: Final[int] = 64


class UnknownToolProfileError(ValueError):
    """The URL names a profile MEHO does not have, or names more than one."""

    def __init__(self, names: list[str]) -> None:
        self.names = names
        known = ", ".join(sorted(TOOL_PROFILES))
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
    if len(names) > 1 or names[0] not in TOOL_PROFILES:
        raise UnknownToolProfileError(names)
    return names[0]


def profile_admits(profile: str | None, tool_name: str) -> bool:
    """Return True when *profile* keeps *tool_name*.

    No profile keeps every tool. An unknown profile keeps none, so a
    mistake fails closed.
    """
    if profile is None:
        return True
    return tool_name in TOOL_PROFILES.get(profile, frozenset())
