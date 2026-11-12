# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""A person on MCP gets the value of an approved secret reveal (``MCP_REVEAL_FETCH``).

A call with ``reveal_secret: true`` on a secret read waits for a second
person's approval. Approving runs nothing: the operation runs once, when the
person who asked fetches the value
(:mod:`meho_backplane.operations.approved_reveal`). On the command line,
in the console and over REST that person has a way to fetch it. This module
gives the same way to a person who works through an AI tool over MCP:

``call_operation(connector_id, op_id, approval_request_id)``

* Only with the switch ``MCP_REVEAL_FETCH`` on (off by default). With it
  off, :func:`reveal_fetch_for_settings` leaves the input and its text out
  of ``call_operation``, so the input check refuses it.
* Only ``connector_id``, ``op_id`` and ``approval_request_id`` may be sent.
  What runs is the stored request, never new params.
* ``connector_id`` and ``op_id`` must be the ones of the request. Otherwise
  the fetch is refused before it uses up the one-time permission.
* An agent is refused, as for ``reveal_secret`` itself.
* The rest is :func:`~meho_backplane.operations.approved_reveal.fetch_approved_reveal`:
  same person and tenant, once, within the fetch window after the approval.
  Each try writes an ``approval.reveal`` audit row.
* When the answer holds the value, the HTTP answer says ``Cache-Control:
  no-store`` (:mod:`meho_backplane.no_store`): the dispatcher or
  :func:`~meho_backplane.operations.approved_reveal.fetch_approved_reveal`
  marks it, and the MCP route sets the headers. A refused or failed run keeps the usual
  headers.

A refused fetch answers ``status="denied"`` with ``extras.error_code``
(``approval_request_not_found``, ``not_request_owner``,
``reveal_wrong_operation``, ``reveal_agent_refused``, or
``reveal_<code>`` of a refused fetch) and ``extras.approval_request_id``.
Nothing ran.
"""

from __future__ import annotations

import uuid
from typing import Any, Final

import structlog

from meho_backplane.auth.delegation import resolve_actor_sub
from meho_backplane.auth.operator import Operator, PrincipalKind
from meho_backplane.connectors.schemas import OperationResult
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.mcp.registry import ToolDefinition
from meho_backplane.mcp.server import McpInvalidParamsError
from meho_backplane.operations.approval_queue import (
    ApprovalNotFoundError,
    ResultAccessForbiddenError,
    get_request,
)
from meho_backplane.operations.approved_reveal import (
    RevealFetchRefusedError,
    fetch_approved_reveal,
    reveal_fetch_window,
)
from meho_backplane.settings import get_settings
from meho_backplane.untrusted_text import with_data_notice

__all__ = [
    "APPROVAL_REQUEST_ID",
    "fetch_reveal_over_mcp",
    "reveal_fetch_for_settings",
]

_log = structlog.get_logger(__name__)

#: The ``call_operation`` input that fetches an approved reveal.
APPROVAL_REQUEST_ID: Final[str] = "approval_request_id"

#: The only inputs a fetch may send.
_FETCH_INPUTS: Final[frozenset[str]] = frozenset({"connector_id", "op_id", APPROVAL_REQUEST_ID})

_PROPERTY: Final[dict[str, Any]] = {
    "type": "string",
    "format": "uuid",
    "description": (
        "Only to get the value of your own approved call with "
        "`reveal_secret: true`: its `extras.approval_request_id`. Send only "
        "`connector_id`, `op_id` and this. MEHO runs the approved call once "
        "and returns the value. A second call is refused."
    ),
}


def _fetch_text() -> str:
    minutes = max(1, int(reveal_fetch_window().total_seconds()) // 60)
    return (
        "\n\nA call with `reveal_secret: true` on a secret read waits for a "
        "second person's approval, and the approval runs nothing. After the "
        "approval, call again with the same `connector_id` and `op_id` and "
        "`approval_request_id`: the call runs once and returns the value. "
        f"This works within {minutes} minutes of the approval, only for the "
        "person who asked."
    )


def reveal_fetch_for_settings(definition: ToolDefinition) -> ToolDefinition:
    """Return ``call_operation`` with the fetch input and its text while the switch is on.

    With ``MCP_REVEAL_FETCH`` off, *definition* comes back unchanged.
    """
    if not get_settings().mcp_reveal_fetch:
        return definition
    schema = definition.inputSchema
    properties = {**schema.get("properties", {}), APPROVAL_REQUEST_ID: _PROPERTY}
    return definition.model_copy(
        update={
            "description": definition.description + _fetch_text(),
            "inputSchema": {**schema, "properties": properties},
        }
    )


def _refused(op_id: str, request_id: uuid.UUID, error_code: str, message: str) -> dict[str, Any]:
    """The answer of a refused fetch: nothing ran, no value."""
    return OperationResult(
        status="denied",
        op_id=op_id,
        error=f"{error_code}: {message}",
        duration_ms=0.0,
        extras={"error_code": error_code, "approval_request_id": str(request_id)},
    ).model_dump(mode="json")


def _parse_request_id(raw: Any) -> uuid.UUID:
    try:
        return uuid.UUID(str(raw))
    except ValueError as exc:
        raise McpInvalidParamsError(
            "`approval_request_id` must be a UUID",
            data={"reason": "approval_request_id_not_uuid"},
        ) from exc


async def _wrong_request_reason(
    operator: Operator, request_id: uuid.UUID, connector_id: str, op_id: str
) -> tuple[str, str] | None:
    """Return ``(error_code, message)`` when the request is not this caller's fetch."""
    try:
        async with get_sessionmaker()() as session:
            row = await get_request(session, tenant_id=operator.tenant_id, request_id=request_id)
    except ApprovalNotFoundError:
        return "approval_request_not_found", "No such request in your tenant."
    if row.principal_sub != operator.sub:
        return "not_request_owner", "Only the person who sent the request can get the value."
    if row.connector_id != connector_id or row.op_id != op_id:
        return (
            "reveal_wrong_operation",
            f"The request is for `{row.op_id}` on `{row.connector_id}`. Send those.",
        )
    return None


async def fetch_reveal_over_mcp(operator: Operator, arguments: dict[str, Any]) -> dict[str, Any]:
    """Run the approved reveal named by ``approval_request_id`` once for the person who asked."""
    if not get_settings().mcp_reveal_fetch:
        raise McpInvalidParamsError(
            "`approval_request_id` is not an input of call_operation here",
            data={"reason": "reveal_fetch_off"},
        )
    extra = sorted(set(arguments) - _FETCH_INPUTS)
    if extra:
        raise McpInvalidParamsError(
            "with `approval_request_id` send only `connector_id` and `op_id`; "
            f"remove: {', '.join(extra)}",
            data={"reason": "reveal_fetch_extra_inputs", "inputs": extra},
        )
    request_id = _parse_request_id(arguments[APPROVAL_REQUEST_ID])
    connector_id = str(arguments.get("connector_id", ""))
    op_id = str(arguments.get("op_id", ""))
    if operator.principal_kind is PrincipalKind.AGENT or resolve_actor_sub() is not None:
        return _refused(
            op_id, request_id, "reveal_agent_refused", "Only a person can get a secret value."
        )
    reason = await _wrong_request_reason(operator, request_id, connector_id, op_id)
    if reason is not None:
        return _refused(op_id, request_id, *reason)
    try:
        result = await fetch_approved_reveal(operator, request_id)
    except ApprovalNotFoundError:
        return _refused(
            op_id, request_id, "approval_request_not_found", "No such request in your tenant."
        )
    except ResultAccessForbiddenError:
        return _refused(
            op_id,
            request_id,
            "not_request_owner",
            "Only the person who sent the request can get the value.",
        )
    except RevealFetchRefusedError as exc:
        return _refused(op_id, request_id, f"reveal_{exc.code}", exc.message)
    # Log only fields that hold no secret value.
    _log.info(
        "mcp_approval_reveal_fetched",
        approval_request_id=str(request_id),
        op_id=result.op_id,
        status=result.status,
        operator_sub=operator.sub,
    )
    return with_data_notice(result.model_dump(mode="json"))
