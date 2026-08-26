# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The human-only MCP surface — decision verbs with no agent-facing path.

Three verbs are deliberately absent from the MCP tool registry under
**every** claim set (including an elevated operator token): approving or
rejecting a parked operation, and granting a time-bounded privilege
elevation to an agent. They are *not* registered by
:mod:`meho_backplane.mcp.tools.approvals` /
:mod:`meho_backplane.mcp.tools.agent_grants`, so they never appear in
``tools/list`` and cannot be dispatched by ``tools/call`` (#3155,
Initiative #3153).

Why these three and not the whole write surface:

* **Approve / reject** exist to force a *human* decision on a parked
  operation (v0.1-spec §7). The same MCP session that parked an op
  must not be able to approve it — a model holding the approve button
  collapses the gate (#3143 F4 evidence: all three were listed for a
  ``tenant_admin`` session).
* **Grant elevation** is model-invocable privilege escalation. A
  registered agent principal must not be able to widen its own (or a
  sibling's) grant window over the agent surface.

The read halves (``meho_approvals_list`` / ``meho_approvals_get``) and
the non-elevation grant verbs (``meho_agents_grant_list`` / ``.show`` /
``.create`` / ``.revoke``) keep their MCP registration — reading a
queue is not deciding it, and grant *administration* stays on the
operator plane per the T1 surface classification (#3154).

This module is the single source of truth for two consumers:

* :func:`meho_backplane.mcp.handlers.handle_tools_call` consults
  :data:`HUMAN_ONLY_MCP_TOOLS` *before* the registry lookup, so an
  accidental future re-registration can never make one of these
  callable — a ``tools/call`` on any of the three is denied with the
  remediation string naming the console / CLI path.
* The guard test ``tests/test_mcp_human_only_surface.py`` asserts none
  of these names is registered and that no tool module wires a handler
  to an approval-decision or grant-elevation endpoint.
"""

from __future__ import annotations

from typing import Final

#: Wire names that have **no** MCP path, mapped to the remediation
#: string the dispatcher returns on a ``tools/call`` attempt. Each
#: message names both the operator console and the ``meho`` CLI verb so
#: an agent that reaches for the removed tool is told exactly where the
#: human decision is made instead.
HUMAN_ONLY_MCP_TOOLS: Final[dict[str, str]] = {
    "meho_approvals_approve": (
        "meho_approvals_approve has no MCP path under any claim set: "
        "approving a parked operation is a human decision (v0.1-spec §7). "
        "Approve via the operator console approvals queue, or the CLI: "
        "`meho approvals approve <request-id>`."
    ),
    "meho_approvals_reject": (
        "meho_approvals_reject has no MCP path under any claim set: "
        "rejecting a parked operation is a human decision (v0.1-spec §7). "
        "Reject via the operator console approvals queue, or the CLI: "
        "`meho approvals reject <request-id> --reason <text>`."
    ),
    "meho_agents_grant_elevate": (
        "meho_agents_grant_elevate has no MCP path under any claim set: "
        "granting a time-bounded privilege elevation is a human-only "
        "operator action. Elevate via the operator console, or the CLI: "
        "`meho agent grant elevate --principal <sub> --op <pattern> "
        "--verdict <v> --expires <iso8601>`."
    ),
}


def human_only_remediation(name: str) -> str | None:
    """Return the remediation string for a human-only tool *name*, else ``None``.

    ``None`` means *name* is not a human-only verb — the caller falls
    through to the normal registry lookup (and its generic
    ``unknown tool`` rejection when the name resolves to nothing).
    """
    return HUMAN_ONLY_MCP_TOOLS.get(name)
