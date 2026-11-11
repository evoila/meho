# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Sign out one tool kind of a person: "Your tools" on the account page.

Each tool kind signs in with its own Keycloak login client: the web
console, the command line, Claude Code (``meho-mcp-code``), Claude Desktop
(``meho-mcp-desktop``) and any other MCP client (``meho-mcp``). So
Keycloak can end the saved login of one (person, client) pair, and the
person's other tools keep working.

* :func:`tool_kinds` -- the tool kinds MEHO knows, with their login client.
* :func:`last_use_by_client` -- the last audit row of the person per login
  client. Audit rows name the client only while ``AUDIT_CLIENT_IDENTITY``
  is on, so with it off no tool has a last use.
* :func:`sign_out_tool` -- ends the person's saved login for one client
  (``DELETE /users/{user id}/consents/{client id}``) and then writes the
  audit row ``auth.tool_sign_out``. A failed Keycloak call writes no row.

The tool's last access token keeps working until it expires: at most about
five minutes on the built-in realm. Signing out is not a block: the tool
can sign in again and gets a new login.

The web console is listed for its last use, but it has no sign-out button
here: the "Active sessions" card of the same page signs out a browser.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Final

import structlog
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from meho_backplane.api.v1.auth_config import MCP_TOOL_CLIENTS
from meho_backplane.auth.delegation import resolve_actor_sub
from meho_backplane.auth.keycloak_admin import KeycloakAdminClient
from meho_backplane.db.models import AuditEventClass, AuditLog
from meho_backplane.memory.audit import INTERNAL_METHOD, write_internal_audit_row
from meho_backplane.request_usage.collector import current_request_id
from meho_backplane.settings import Settings

__all__ = [
    "LAST_USE_WINDOW",
    "OTHER_MCP_CLIENT_ID",
    "TOOL_SIGN_OUT_AUDIT_PATH",
    "ToolKind",
    "last_use_by_client",
    "sign_out_tool",
    "tool_kinds",
]

_log = structlog.get_logger(__name__)

#: The ``path`` of the audit row a tool sign-out writes.
TOOL_SIGN_OUT_AUDIT_PATH: Final[str] = "auth.tool_sign_out"

#: How far back :func:`last_use_by_client` looks.
LAST_USE_WINDOW: Final[timedelta] = timedelta(days=90)

#: The shared login client of MCP clients that have no client of their own.
OTHER_MCP_CLIENT_ID: Final[str] = "meho-mcp"


@dataclass(frozen=True)
class ToolKind:
    """One tool kind and the Keycloak login client it signs in with."""

    key: str
    label: str
    client_id: str
    can_sign_out: bool


def tool_kinds(settings: Settings) -> list[ToolKind]:
    """Return the tool kinds MEHO knows, in page order.

    A kind whose client id is not set (no web console client, no command
    line client) is left out. A client id is listed once.
    """
    candidates = [
        ToolKind("web", "Web console", settings.ui_keycloak_client_id, False),
        ToolKind("cli", "Command line (meho)", settings.keycloak_cli_client_id, True),
        ToolKind("claude-code", "Claude Code", MCP_TOOL_CLIENTS["claude-code"], True),
        ToolKind("claude-desktop", "Claude Desktop", MCP_TOOL_CLIENTS["claude-desktop"], True),
        ToolKind("other-mcp", "Other MCP client", OTHER_MCP_CLIENT_ID, True),
    ]
    kinds: list[ToolKind] = []
    seen: set[str] = set()
    for kind in candidates:
        if kind.client_id and kind.client_id not in seen:
            seen.add(kind.client_id)
            kinds.append(kind)
    return kinds


async def last_use_by_client(
    db_session: AsyncSession,
    *,
    operator_sub: str,
    client_ids: Iterable[str],
    now: datetime,
) -> dict[str, datetime]:
    """Return the newest audit row time of *operator_sub* per login client.

    Reads only the person's own rows of the last :data:`LAST_USE_WINDOW`,
    in every tenant (a login is per person, not per tenant). A client with
    no such row is missing from the answer.
    """
    wanted = list(client_ids)
    if not wanted:
        return {}
    azp = AuditLog.payload[("client", "azp")].as_string()
    stmt = (
        select(azp, func.max(AuditLog.occurred_at))
        .where(
            AuditLog.operator_sub == operator_sub,
            AuditLog.occurred_at >= now - LAST_USE_WINDOW,
            azp.in_(wanted),
        )
        .group_by(azp)
    )
    result = await db_session.execute(stmt)
    return {str(client): last for client, last in result.all() if last is not None}


async def sign_out_tool(
    *,
    operator_sub: str,
    tenant_id: uuid.UUID,
    kind: ToolKind,
    settings: Settings,
) -> uuid.UUID:
    """End the saved login of *operator_sub* for *kind* and write the audit row.

    *operator_sub* is the Keycloak user id of the person. The Keycloak
    call runs first; the audit row is written only after it succeeded.
    Every :class:`~meho_backplane.auth.keycloak_admin.KeycloakAdminError`
    (also "not configured" and "nothing to sign out") reaches the caller,
    and then no row is written. Returns the id of the audit row.
    """
    started = time.monotonic()
    async with KeycloakAdminClient.from_settings(settings) as kc:
        await kc.revoke_user_client_consent(operator_sub, kind.client_id)
    audit_id = await write_internal_audit_row(
        event_class=AuditEventClass.OPERATION_EXECUTION,
        operator_sub=operator_sub,
        actor_sub=resolve_actor_sub(),
        tenant_id=tenant_id,
        method=INTERNAL_METHOD,
        path=TOOL_SIGN_OUT_AUDIT_PATH,
        status_code=200,
        request_id=current_request_id(),
        duration_ms=(time.monotonic() - started) * 1000,
        payload={"client_id": kind.client_id, "tool": kind.key},
    )
    _log.info(
        "tool_sign_out",
        operator_sub=operator_sub,
        tenant_id=str(tenant_id),
        client_id=kind.client_id,
    )
    return audit_id
