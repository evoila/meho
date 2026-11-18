# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""A settings import asks a person to approve each target it creates or changes.

A new target and a change of a target always wait for a person
(``targets.register`` and ``targets.update``, see
:mod:`meho_backplane.connectors.targets.ops`). A settings import holds the
same writes, so it does not write targets itself. For each target the file
creates or changes, :func:`ask_for_target_writes` sends one call of the
matching operation through the dispatcher, as the person who imports. The
policy gate makes each call wait, and the import answers with one approval
request per target. Each approval card shows the target's values: the new
values for a new target, the old and the new value of each changed field
for a changed one. The target is written only when a person approves its
request. An import that creates or changes no target writes no request.

When a request for the same call already waits (a second import of the
same file, for example), the import makes no new request. It lists the
waiting request with ``already_waiting`` set, so the caller sees its id. The
call is the same only when the operation and every param match the waiting
request. When a request for the same target waits with other values, the
import sends nothing for that target and says so in ``failed``: a person
approves or rejects the waiting request first, then imports again. A call
that runs at once is listed in ``written``.

The calls run before the import writes anything else, so a request row and
the import's own transaction never wait for each other. A target in a tenant
that the same file creates waits for the next import: its tenant does not
exist yet when the request is made.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any

import structlog
from sqlalchemy import select

from meho_backplane.auth.operator import Operator
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import ApprovalRequest, ApprovalRequestStatus
from meho_backplane.instance_settings.schema import contains_secret
from meho_backplane.targets.schemas import TargetCreate, TargetUpdate

__all__ = ["TargetWrite", "ask_for_target_writes"]

_log = structlog.get_logger(__name__)


@dataclass
class TargetWrite:
    """One target the file creates or changes, and where it is in the file."""

    path: str
    name: str
    body: TargetCreate | TargetUpdate
    #: The id of the stored target for a change; ``None`` for a new target.
    target_id: uuid.UUID | None = None
    #: Whether the target's tenant exists before the import runs.
    tenant_exists: bool = True


@dataclass
class TargetAsks:
    """What :func:`ask_for_target_writes` did: the requests and the failures."""

    waiting: list[dict[str, Any]] = field(default_factory=list)
    #: Calls that ran at once (``path``, ``name``, ``op_id``).
    written: list[dict[str, Any]] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)


def _call_of(write: TargetWrite) -> tuple[str, dict[str, Any]]:
    """The operation and its params for *write*: only the fields the file sets."""
    # Imported here: the targets connector imports the REST targets module,
    # which the settings service imports too.
    from meho_backplane.connectors.targets.ops import TARGETS_REGISTER_OP_ID
    from meho_backplane.connectors.targets.update import TARGETS_UPDATE_OP_ID

    sent = write.body.model_dump(mode="json", exclude_unset=True)
    if isinstance(write.body, TargetCreate):
        return TARGETS_REGISTER_OP_ID, sent
    return TARGETS_UPDATE_OP_ID, {"target_id": str(write.target_id), **sent}


def _same_target(request: ApprovalRequest, write: TargetWrite) -> bool:
    params = request.params or {}
    if write.target_id is not None and params.get("target_id") == str(write.target_id):
        return True
    return write.target_id is None and params.get("name") == write.name


@dataclass(frozen=True)
class _Waiting:
    """A request for the same target that still waits."""

    request_id: uuid.UUID
    #: Whether the request asks for the same call: same operation, same params.
    same_call: bool


async def _waiting_request(
    tenant_id: uuid.UUID, write: TargetWrite, op_id: str, params: dict[str, Any]
) -> _Waiting | None:
    """A request for the same target that still waits, or ``None``.

    A request for the same call comes first. Else the oldest request for
    the same target is returned with ``same_call`` false.
    """
    from meho_backplane.connectors.targets.ops import (
        TARGETS_REGISTER_OP_ID,
        TARGETS_REGISTRY_CONNECTOR_ID,
    )
    from meho_backplane.connectors.targets.update import TARGETS_UPDATE_OP_ID
    from meho_backplane.operations._validate import params_hash_matches
    from meho_backplane.operations.approval_queue import _is_overdue_pending

    other: _Waiting | None = None
    async with get_sessionmaker()() as session:
        rows = await session.scalars(
            select(ApprovalRequest)
            .where(
                ApprovalRequest.tenant_id == tenant_id,
                ApprovalRequest.connector_id == TARGETS_REGISTRY_CONNECTOR_ID,
                ApprovalRequest.op_id.in_((TARGETS_REGISTER_OP_ID, TARGETS_UPDATE_OP_ID)),
                ApprovalRequest.status == ApprovalRequestStatus.PENDING.value,
            )
            .order_by(ApprovalRequest.created_at)
        )
        for row in rows:
            if not _same_target(row, write) or _is_overdue_pending(row):
                continue
            if row.op_id == op_id and params_hash_matches(params, row.params_hash):
                return _Waiting(row.id, same_call=True)
            if other is None:
                other = _Waiting(row.id, same_call=False)
    return other


async def _ask(operator: Operator, write: TargetWrite, asks: TargetAsks) -> None:
    from meho_backplane.connectors.targets.ops import TARGETS_REGISTRY_CONNECTOR_ID
    from meho_backplane.operations.dispatcher import dispatch

    op_id, params = _call_of(write)
    entry: dict[str, Any] = {"path": write.path, "name": write.name, "op_id": op_id}
    waiting = await _waiting_request(operator.tenant_id, write, op_id, params)
    if waiting is not None and waiting.same_call:
        asks.waiting.append(
            {**entry, "approval_request_id": str(waiting.request_id), "already_waiting": True}
        )
        return
    if waiting is not None:
        asks.failed.append(
            f"`{write.path}` ({write.name}) was not sent for approval: request "
            f"{waiting.request_id} for this target waits with other values. Approve "
            "or reject that request, then import the file again."
        )
        return
    result = await dispatch(
        operator=operator,
        connector_id=TARGETS_REGISTRY_CONNECTOR_ID,
        op_id=op_id,
        target=None,
        params=params,
    )
    if result.status == "awaiting_approval":
        asks.waiting.append(
            {**entry, "approval_request_id": str(result.extras["approval_request_id"])}
        )
        return
    if result.status == "ok":
        asks.written.append(entry)
        return
    message = str(result.extras.get("exception_message") or result.error or result.status)
    reason = "" if contains_secret(message) else f": {message.strip().rstrip('.')}"
    _log.warning(
        "settings_import_target_request_failed",
        name=write.name,
        op_id=op_id,
        status=result.status,
    )
    asks.failed.append(f"`{write.path}` ({write.name}) could not be sent for approval{reason}.")


async def ask_for_target_writes(
    operators: dict[uuid.UUID, Operator], writes: dict[uuid.UUID, list[TargetWrite]]
) -> TargetAsks:
    """Send each target write of the import for approval; write no target.

    *writes* maps a tenant id to the target writes of that tenant, and
    *operators* maps it to the importing person as seen in that tenant.
    Returns the waiting requests (``path``, ``name``, ``op_id``,
    ``approval_request_id``, and ``already_waiting`` for a request an earlier
    call made), the calls that ran at once, and one sentence per write that
    could not be sent. A failure never stops the other writes.
    """
    asks = TargetAsks()
    for tenant_id, tenant_writes in writes.items():
        for write in tenant_writes:
            if not write.tenant_exists:
                asks.failed.append(
                    f"`{write.path}` ({write.name}) was not sent for approval: its "
                    "tenant is new. Import the file again to send it."
                )
                continue
            await _ask(operators[tenant_id], write, asks)
    return asks
