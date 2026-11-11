# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Sign out one tool kind of a person: "Your tools" on the account page.

Each tool kind signs in with its own Keycloak login client: the web
console, the command line, Claude Code (``meho-mcp-code``), Claude Desktop
(``meho-mcp-desktop``) and any other MCP client (``meho-mcp``). So
Keycloak can end the logins of one (person, client) pair, and the
person's other tools keep working.

* :func:`tool_kinds` -- the tool kinds MEHO knows, with their login client.
* :func:`last_use_by_client` -- the last audit row of the person per login
  client. Audit rows name the client only while ``AUDIT_CLIENT_IDENTITY``
  is on, so with it off no tool has a last use.
* :func:`end_tool_logins` -- the Keycloak part. It ends both kinds of
  login of the person for one client:

  - the consent and offline login
    (``DELETE /users/{user id}/consents/{client id}``);
  - each ordinary (online) login that holds only this client
    (``GET /users/{user id}/sessions``, then ``DELETE /sessions/{id}``).

  Keycloak can end an ordinary login only as a whole. A login that this
  client shares with another client (both signed in through one browser
  login) is kept, so the other tool keeps working. The answer names those
  other clients, and the page tells the person.
* :func:`sign_out_tool` -- runs :func:`end_tool_logins` and then writes the
  audit row ``auth.tool_sign_out``. A failed Keycloak call writes a row
  too (status 502): it says what ended before the failure, if anything,
  and which error stopped it.

The tool's last access token keeps working until it expires: the access
token lifetime of the realm (about five minutes on MEHO's built-in
Keycloak). Signing out is not a block: the tool can sign in again and gets
a new login.

The web console is listed for its last use, but it has no sign-out button
here: the "Active sessions" card of the same page signs out a browser.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Iterable
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from typing import Any, Final

import structlog
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from meho_backplane.api.v1.auth_config import MCP_TOOL_CLIENTS
from meho_backplane.auth.delegation import resolve_actor_sub
from meho_backplane.auth.keycloak_admin import (
    KeycloakAdminClient,
    KeycloakAdminError,
    KeycloakConsentNotFoundError,
)
from meho_backplane.db.models import AuditEventClass, AuditLog
from meho_backplane.memory.audit import INTERNAL_METHOD, write_internal_audit_row
from meho_backplane.request_usage.collector import current_request_id
from meho_backplane.settings import Settings

__all__ = [
    "LAST_USE_WINDOW",
    "OTHER_MCP_CLIENT_ID",
    "TOOL_SIGN_OUT_AUDIT_PATH",
    "ToolKind",
    "ToolLoginsEnded",
    "ToolSignOutIncompleteError",
    "end_tool_logins",
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


@dataclass(frozen=True)
class ToolLoginsEnded:
    """What :func:`end_tool_logins` ended, and what it kept."""

    offline_login_ended: bool
    sessions_ended: int
    shared_with: tuple[str, ...]
    audit_id: uuid.UUID | None = None

    @property
    def ended_any(self) -> bool:
        return self.offline_login_ended or self.sessions_ended > 0


_NOTHING_ENDED: Final[ToolLoginsEnded] = ToolLoginsEnded(False, 0, ())


class ToolSignOutIncompleteError(KeycloakAdminError):
    """A Keycloak call failed after some logins of the tool already ended.

    ``ended`` says what ended before the failure. The failed call is the
    ``__cause__``.
    """

    def __init__(self, ended: ToolLoginsEnded) -> None:
        super().__init__("Keycloak failed after part of the tool's logins ended")
        self.ended = ended


def _split_sessions(
    sessions: list[dict[str, Any]], client_id: str
) -> tuple[list[str], tuple[str, ...]]:
    """Return the ids of the logins that hold only *client_id*, and the
    other clients of the logins that *client_id* shares with them."""
    own: list[str] = []
    shared_with: set[str] = set()
    for session in sessions:
        clients = {str(c) for c in (session.get("clients") or {}).values()}
        if client_id not in clients:
            continue
        others = clients - {client_id}
        if others:
            shared_with.update(others)
        elif session.get("id"):
            own.append(str(session["id"]))
    return own, tuple(sorted(shared_with))


async def end_tool_logins(
    kc: KeycloakAdminClient, *, user_id: str, client_id: str
) -> ToolLoginsEnded:
    """End the logins of user *user_id* for login client *client_id* only.

    Ends the consent and offline login, and each ordinary login that holds
    no other client. Keeps each ordinary login that also holds another
    client, and names those other clients in ``shared_with``. Raises
    :class:`~meho_backplane.auth.keycloak_admin.KeycloakConsentNotFoundError`
    when the person has no login of this client at all, and
    :class:`ToolSignOutIncompleteError` when a call fails after a login
    already ended.
    """
    own_sessions, shared = _split_sessions(await kc.list_user_sessions(user_id), client_id)
    offline_login_ended = False
    sessions_ended = 0
    try:
        try:
            await kc.revoke_user_client_consent(user_id, client_id)
            offline_login_ended = True
        except KeycloakConsentNotFoundError:
            pass
        for session_id in own_sessions:
            if await kc.delete_user_session(session_id):
                sessions_ended += 1
    except KeycloakAdminError as exc:
        if offline_login_ended or sessions_ended:
            raise ToolSignOutIncompleteError(
                ToolLoginsEnded(offline_login_ended, sessions_ended, shared)
            ) from exc
        raise
    if not offline_login_ended and sessions_ended == 0 and not shared:
        raise KeycloakConsentNotFoundError(
            f"Keycloak has no login of this person for client {client_id!r}"
        )
    return ToolLoginsEnded(offline_login_ended, sessions_ended, shared)


async def sign_out_tool(
    *,
    operator_sub: str,
    tenant_id: uuid.UUID,
    kind: ToolKind,
    settings: Settings,
) -> ToolLoginsEnded:
    """End the logins of *operator_sub* for *kind* and write the audit row.

    *operator_sub* is the Keycloak user id of the person. The Keycloak
    calls run first, then the audit row is written:

    * at least one login ended: a row with status 200;
    * a Keycloak call failed (also "not configured"): a row with status
      502 that names the error and what ended before it. Then the error
      reaches the caller;
    * no login at all ("nothing to sign out"), or only shared logins:
      nothing ended and no row is written (``audit_id`` is ``None``).
    """
    started = time.monotonic()
    try:
        async with KeycloakAdminClient.from_settings(settings) as kc:
            ended = await end_tool_logins(kc, user_id=operator_sub, client_id=kind.client_id)
    except KeycloakConsentNotFoundError:
        raise
    except KeycloakAdminError as exc:
        partial = exc.ended if isinstance(exc, ToolSignOutIncompleteError) else None
        cause = exc.__cause__ if partial is not None else exc
        await _write_sign_out_row(
            operator_sub=operator_sub,
            tenant_id=tenant_id,
            kind=kind,
            ended=partial or _NOTHING_ENDED,
            started=started,
            outcome="partly_signed_out" if partial is not None else "failed",
            error=type(cause).__name__,
        )
        raise
    if not ended.ended_any:
        return ended
    audit_id = await _write_sign_out_row(
        operator_sub=operator_sub,
        tenant_id=tenant_id,
        kind=kind,
        ended=ended,
        started=started,
        outcome="signed_out",
    )
    _log.info(
        "tool_sign_out",
        operator_sub=operator_sub,
        tenant_id=str(tenant_id),
        client_id=kind.client_id,
        sessions_ended=ended.sessions_ended,
    )
    return replace(ended, audit_id=audit_id)


async def _write_sign_out_row(
    *,
    operator_sub: str,
    tenant_id: uuid.UUID,
    kind: ToolKind,
    ended: ToolLoginsEnded,
    started: float,
    outcome: str,
    error: str | None = None,
) -> uuid.UUID:
    """Write one ``auth.tool_sign_out`` row: status 200, or 502 with *error*."""
    payload: dict[str, object] = {
        "client_id": kind.client_id,
        "tool": kind.key,
        "outcome": outcome,
        "offline_login_ended": ended.offline_login_ended,
        "sessions_ended": ended.sessions_ended,
        "shared_with": list(ended.shared_with),
    }
    if error is not None:
        payload["error"] = error
    return await write_internal_audit_row(
        event_class=AuditEventClass.OPERATION_EXECUTION,
        operator_sub=operator_sub,
        actor_sub=resolve_actor_sub(),
        tenant_id=tenant_id,
        method=INTERNAL_METHOD,
        path=TOOL_SIGN_OUT_AUDIT_PATH,
        status_code=200 if error is None else 502,
        request_id=current_request_id(),
        duration_ms=(time.monotonic() - started) * 1000,
        payload=payload,
    )
