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

This is the **MCP-transport** half of one transport-independent policy:
approval is a human decision (v0.1-spec §7). The **REST-transport** half
lives in :func:`meho_backplane.auth.rbac.ensure_human_principal` /
:func:`~meho_backplane.auth.rbac.require_human_principal`, which reject a
machine ``principal_kind`` (agent / service / runner) on the approval
decision routes and on grant create / elevate + agent-principal register —
closing the seam that an agent principal, minted ``tenant_admin`` with the
REST audience, would otherwise reach over REST (meho-internal#289). Both
halves consult the same
:func:`meho_backplane.auth.operator.is_human_principal` split, so removing
a verb from the agent surface here and refusing the machine kind there are
one decision, not two.

Two registered operator-plane tools are for people too, while their read
and revoke siblings stay open to machine logins with the role:
``meho_agents_grant_create`` and ``meho_agent_principals_register``. Only
a person creates an agent grant or registers an agent principal, the same
as on REST. Their handlers call :func:`ensure_human_mcp_caller` first, so
a service login with ``tenant_admin`` and ``mcp:admin`` is refused. The
login kind comes from the token, also when MEHO infers it (a
client-credentials token without a ``principal_kind`` claim is a service
login).
"""

from __future__ import annotations

from typing import Final

import structlog

from meho_backplane.auth.operator import Operator, is_human_principal
from meho_backplane.mcp.server import McpPermissionDeniedError

_log = structlog.get_logger(__name__)

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


def ensure_human_mcp_caller(operator: Operator, tool: str, human_path: str) -> None:
    """Refuse a machine login on a tool that only a person may call.

    A person (``principal_kind=user``) passes. An agent, service or runner
    login gets :class:`~meho_backplane.mcp.server.McpPermissionDeniedError`
    (JSON-RPC ``-32602``, audited as a 403) with the
    ``human_principal_required:`` prefix that REST uses, and *human_path*,
    the text that names where a person does it instead.
    """
    if is_human_principal(operator):
        return
    _log.warning(
        "mcp_tool_call_human_principal_required",
        tool=tool,
        operator_sub=operator.sub,
        principal_kind=operator.principal_kind.value,
    )
    raise McpPermissionDeniedError(
        f"human_principal_required: {tool} is for people only. A machine login "
        f"(agent, service or runner) cannot call it. {human_path}"
    )
