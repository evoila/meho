# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The kind of caller on the audit row, with ``CUSTOM_POLICY`` on.

With the switch on, every audit row of a call names who called, in the
column ``audit_log.caller_kind``: ``person``, ``personal_agent``,
``service_agent``, ``runner`` or ``system``
(:func:`~meho_backplane.auth.caller_kind.caller_kind`). A
``personal_agent`` row also gets the payload block ``caller``::

    {"azp": "meho-cli", "tool": "Claude Code"}

* ``azp`` -- the login client the token was made for (the ``azp`` claim).
* ``tool`` -- the name the MCP client sent about itself in ``initialize``
  (``clientInfo.name``), cleaned by
  :func:`~meho_backplane.auth.client_identity.clean_client_text`. ``None``
  outside an MCP session. MEHO records it and decides nothing with it.

The block is written whenever ``CUSTOM_POLICY`` is on, also with
``AUDIT_CLIENT_IDENTITY`` off. That switch keeps its own ``client`` block,
and this module does not change it.

Three writers take the login directly and call :func:`caller_columns` and
:func:`apply_caller_block`: the operation rows, the MCP rows and the
approval rows. Every other row gets its kind from the listener
:func:`_fill_caller_kind`, when the row is built:

* a row under MEHO's own name (``system`` or ``system:<job>``) gets
  ``system`` (a writer with another name of its own uses
  :func:`system_columns`);
* a row under the name of the login of the current request (bound by the
  token check with :func:`bind_request_caller`) gets that login's kind, or
  ``service_agent`` when an agent acted for the login (``actor_sub``).

Any other row keeps ``NULL``. With the switch off, every row keeps
``NULL`` and no row gets the block.
"""

from __future__ import annotations

import uuid
from contextvars import ContextVar
from typing import Any, Final, NamedTuple

from sqlalchemy import event, select

from meho_backplane.auth.caller_kind import caller_kind
from meho_backplane.auth.client_identity import clean_client_text
from meho_backplane.auth.delegation import resolve_actor_sub
from meho_backplane.auth.operator import CallerKind, Operator
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import AuditLog
from meho_backplane.policy.switches import custom_policy_on
from meho_backplane.settings import get_settings

__all__ = [
    "CALLER_KIND_KEY",
    "CALLER_PAYLOAD_KEY",
    "MCP_SESSION_OPEN_PATH",
    "apply_caller_block",
    "bind_mcp_tool_name",
    "bind_request_caller",
    "caller_columns",
    "caller_record_on",
    "is_system_name",
    "reset_request_caller",
    "session_tool_name",
    "system_columns",
]

#: The payload key of the block.
CALLER_PAYLOAD_KEY: Final[str] = "caller"

#: The key of the requester's kind in ``approval_request.proposed_effect``.
CALLER_KIND_KEY: Final[str] = "caller_kind"

#: The path of the ``mcp.session_open`` row, which keeps the MCP client name.
MCP_SESSION_OPEN_PATH: Final[str] = "/mcp/initialize"

#: MEHO's own name on the rows of a job with no login.
_SYSTEM_NAME: Final[str] = "system"


class _BoundCaller(NamedTuple):
    """The login of the current request, for the rows its writers build."""

    sub: str
    kind: CallerKind
    azp: str | None


#: The login of the current request. Set by the token check, reset by the
#: audit middleware at the start of each request.
_request_caller: ContextVar[_BoundCaller | None] = ContextVar("audit_request_caller", default=None)

#: The MCP client name of the current MCP request, when there is one.
_mcp_tool_name: ContextVar[str | None] = ContextVar("audit_mcp_tool_name", default=None)


def caller_record_on() -> bool:
    """True when ``CUSTOM_POLICY`` is on."""
    return custom_policy_on(get_settings())


def is_system_name(sub: object) -> bool:
    """True for MEHO's own name: ``system`` or ``system:<job>``."""
    return isinstance(sub, str) and (sub == _SYSTEM_NAME or sub.startswith(f"{_SYSTEM_NAME}:"))


def caller_columns(operator: Operator) -> dict[str, Any]:
    """``{"caller_kind": <kind>}`` for a row of *operator*, or ``{}`` with the switch off."""
    if not caller_record_on():
        return {}
    return {"caller_kind": caller_kind(operator, resolve_actor_sub())}


def system_columns() -> dict[str, Any]:
    """``{"caller_kind": "system"}`` for a row of MEHO's own code; ``{}`` with the switch off.

    For a writer whose row names none of MEHO's own ``system`` names (the
    event intake's ``__ingest__``).
    """
    return {"caller_kind": "system"} if caller_record_on() else {}


def _block(azp: str | None) -> dict[str, Any]:
    return {"azp": azp, "tool": _mcp_tool_name.get()}


def apply_caller_block(payload: dict[str, Any], operator: Operator) -> None:
    """Put the ``caller`` block into *payload* for a ``personal_agent`` row.

    The block comes from the checked login, so it replaces any ``caller``
    key that came another way. Nothing happens with the switch off.
    """
    if not caller_record_on():
        return
    if caller_kind(operator, resolve_actor_sub()) == "personal_agent":
        payload[CALLER_PAYLOAD_KEY] = _block(operator.azp)
    else:
        payload.pop(CALLER_PAYLOAD_KEY, None)


def bind_request_caller(operator: Operator) -> None:
    """Keep the kind of *operator* for the rows of this request."""
    if caller_record_on():
        _request_caller.set(_BoundCaller(operator.sub, caller_kind(operator, None), operator.azp))


def reset_request_caller() -> None:
    """Forget the login and the MCP client name of an earlier request."""
    _request_caller.set(None)
    _mcp_tool_name.set(None)


def bind_mcp_tool_name(name: object) -> None:
    """Keep the MCP client name of this request, cleaned."""
    _mcp_tool_name.set(clean_client_text(name))


async def session_tool_name(operator: Operator, session_id: uuid.UUID) -> str | None:
    """The MCP client name of the session *session_id* of *operator*, or ``None``.

    Read from the ``mcp.session_open`` row of the session, written in the
    same tenant under the same login. A session that started before the
    switch was on has no such row.
    """
    query = (
        select(AuditLog.payload)
        .where(
            AuditLog.agent_session_id == session_id,
            AuditLog.path == MCP_SESSION_OPEN_PATH,
            AuditLog.operator_sub == operator.sub,
            AuditLog.tenant_id == operator.tenant_id,
        )
        .order_by(AuditLog.occurred_at)
        .limit(1)
    )
    async with get_sessionmaker()() as session:
        payload = (await session.execute(query)).scalar_one_or_none()
    if not isinstance(payload, dict):
        return None
    return clean_client_text(payload.get("client_name"))


def _kind_of_new_row(kwargs: dict[str, Any]) -> CallerKind | None:
    """The kind of a row its writer built without one, or ``None``."""
    operator_sub = kwargs.get("operator_sub")
    if is_system_name(operator_sub):
        return "system"
    bound = _request_caller.get()
    if bound is None or bound.sub != operator_sub:
        return None
    return "service_agent" if kwargs.get("actor_sub") else bound.kind


@event.listens_for(AuditLog, "init")
def _fill_caller_kind(_target: AuditLog, _args: Any, kwargs: dict[str, Any]) -> None:
    """Give a row the kind of its caller when its writer left it out.

    A ``personal_agent`` row also gets the ``caller`` block when its
    payload has none. The payload is copied, not changed in place: the
    writer may still hold it. SQLAlchemy passes ``kwargs`` so that the
    listener may change it before ``__init__`` runs.
    """
    if kwargs.get("caller_kind") is not None or not caller_record_on():
        return
    kind = _kind_of_new_row(kwargs)
    if kind is None:
        return
    kwargs["caller_kind"] = kind
    bound = _request_caller.get()
    payload = kwargs.get("payload")
    if kind != "personal_agent" or bound is None or not isinstance(payload, dict):
        return
    if CALLER_PAYLOAD_KEY not in payload:
        kwargs["payload"] = {**payload, CALLER_PAYLOAD_KEY: _block(bound.azp)}
