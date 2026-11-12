# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Short codes that say why MEHO refused a call.

A refused call gets a text that says why, for the caller. The audit row
of the refusal keeps the same text and one of these codes, so a reader of
the audit log can tell which rule refused the call without reading the
text.

* ``agent_grant_denied`` — an agent's grants (or its safety default)
  refused the call. In a run that a person started, this is the agent's
  own grant, not the person's.
* ``agent_no_grant`` — an agent has no grant that covers the call, and
  ``AGENT_GRANT_REQUIRED=on`` says it needs one (#6189).
* ``agent_steps_not_declared`` — an agent called a multi-step operation
  that changes something, and its connector does not list the steps. With
  ``AGENT_GRANT_REQUIRED=on`` MEHO cannot check the agent's grants for each
  step, so it refuses the call before it starts (#6298).
* ``role_insufficient`` — the caller's role is below what the operation
  or tool needs.
* ``read_scope_denied`` — a service login's read scope does not cover the
  target.
* ``connector_not_allowed`` — the agent may not use this connector.
* ``agent_reveal_refused`` — an agent asked for hidden values; only a
  person or a service login may get them.
* ``policy_denied`` — any other refusal.

The codes and texts hold no secret: they name operations, grant patterns,
roles and logins only.
"""

from __future__ import annotations

from typing import Final

__all__ = [
    "AGENT_GRANT_DENIED",
    "AGENT_NO_GRANT",
    "AGENT_REVEAL_REFUSED",
    "AGENT_STEPS_NOT_DECLARED",
    "CONNECTOR_NOT_ALLOWED",
    "POLICY_DENIED",
    "READ_SCOPE_DENIED",
    "ROLE_INSUFFICIENT",
    "deny_reason_code",
    "is_agent_no_grant",
]

AGENT_GRANT_DENIED: Final[str] = "agent_grant_denied"
AGENT_NO_GRANT: Final[str] = "agent_no_grant"
AGENT_STEPS_NOT_DECLARED: Final[str] = "agent_steps_not_declared"
ROLE_INSUFFICIENT: Final[str] = "role_insufficient"
READ_SCOPE_DENIED: Final[str] = "read_scope_denied"
CONNECTOR_NOT_ALLOWED: Final[str] = "connector_not_allowed"
AGENT_REVEAL_REFUSED: Final[str] = "agent_reveal_refused"
POLICY_DENIED: Final[str] = "policy_denied"

#: The agent grant resolver (``auth/permissions.py``) starts its text with
#: the verdict; a direct run puts "agent <name> acting for <sub>: " first.
_AGENT_VERDICT_MARK: Final[str] = "verdict=deny"
#: The role floor text (``operations/role_floors.py``).
_ROLE_FLOOR_MARK: Final[str] = " role; your role is "


def is_agent_no_grant(reason: str) -> bool:
    """Say whether *reason* is the "agent has no grant" refusal.

    The resolver starts that text with ``agent_no_grant:``. A direct run
    puts "agent <name> acting for <sub>: " before it, so the code may
    follow that prefix.
    """
    return reason.startswith(f"{AGENT_NO_GRANT}:") or f": {AGENT_NO_GRANT}:" in reason


def deny_reason_code(reason: str) -> str:
    """Return the code for a deny *reason* text of the policy gate.

    The gate returns a text, not a code. Each rule that can refuse a call
    writes its text in one fixed form, so the form tells the rule apart.
    A text of no known form gives ``policy_denied``.
    """
    if reason.startswith(f"{READ_SCOPE_DENIED}:"):
        return READ_SCOPE_DENIED
    if reason.startswith(f"{AGENT_STEPS_NOT_DECLARED}:"):
        return AGENT_STEPS_NOT_DECLARED
    if is_agent_no_grant(reason):
        return AGENT_NO_GRANT
    if _AGENT_VERDICT_MARK in reason:
        return AGENT_GRANT_DENIED
    if _ROLE_FLOOR_MARK in reason:
        return ROLE_INSUFFICIENT
    return POLICY_DENIED
