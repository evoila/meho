# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The "Stored session" part of the audit drawer (#4904).

An audit row that belongs to a stored agent session links to it:

* a hosted agent run links to its run page (``/ui/agents/runs/{run id}``);
* an uploaded session shows its id and the command that reads it
  (``meho sessions show <id>``). The console has no page for it yet.

Only a viewer who may read sessions
(:func:`~meho_backplane.session_store.audit_links.may_read_sessions`) gets
the part, and only with the switch ``AUDIT_SESSION_LINKS`` on. For anyone
else the drawer leaves it out, the same as for a row with no stored
session, so it gives no hint that a session exists.
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from meho_backplane.auth.operator import TenantRole
from meho_backplane.db.models import AuditLog
from meho_backplane.session_store.audit_links import (
    may_read_sessions,
    session_links_on,
    stored_session_for,
)
from meho_backplane.ui.auth.middleware import UISessionContext
from meho_backplane.ui.roles import resolve_ui_operator

__all__ = ["drawer_rights", "stored_session_context"]


async def drawer_rights(session: UISessionContext) -> tuple[bool, bool]:
    """Return whether the viewer is a tenant admin, and whether it may read sessions.

    One role lift for both. A failed lift gives ``(False, False)``.
    """
    operator = await resolve_ui_operator(session)
    if operator is None:
        return False, False
    return operator.tenant_role is TenantRole.TENANT_ADMIN, may_read_sessions(operator)


async def stored_session_context(
    db_session: AsyncSession, row: AuditLog, *, may_read: bool
) -> dict[str, object] | None:
    """Return the drawer's "Stored session" values for *row*, or ``None``."""
    if not may_read or row.tenant_id is None or not session_links_on():
        return None
    link = await stored_session_for(
        db_session,
        tenant_id=row.tenant_id,
        audit_id=row.id,
        agent_session_id=row.agent_session_id,
        payload=row.payload,
    )
    if link is None:
        return None
    return {
        "session_id": link.session_id,
        "href": f"/ui/agents/runs/{link.session_id}" if link.hosted_run else None,
        "command": f"meho sessions show {link.session_id}",
    }
