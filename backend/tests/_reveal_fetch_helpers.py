# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Shared steps of the approved-reveal fetch tests (no-store headers, MCP fetch).

A person asks to see a secret value of ``vault.kv.read``
(``reveal_secret: true``). The call waits for approval; a second person
approves it, which runs nothing. The tests then fetch the value.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock
from uuid import UUID

import pytest

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors.base import Connector
from meho_backplane.connectors.registry import register_connector_v2
from meho_backplane.connectors.schemas import FingerprintResult, OperationResult, ProbeResult
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import ApprovalRequest
from meho_backplane.operations import approval_queue, dispatch, register_typed_operation
from meho_backplane.operations.approval_queue import (
    approve_request,
    resume_dispatch_after_approval,
)
from meho_backplane.operations.approved_reveal import WAITING_FOR_REQUESTER
from meho_backplane.retrieval.embedding import EMBEDDING_DIMENSION

#: A made-up secret value. Built from parts so secret scanners do not flag it.
SECRET_VALUE = "made-up" + "-password-6273"
CONNECTOR_ID = "vault-1.x"
READ_OP = "vault.kv.read"
#: A plain read (not a credential read) whose answer has a secret-named field.
#: A person's ``reveal_secret: true`` on it runs at once, with no approval.
PLAIN_OP = "vault.app.config"
#: An operation that makes a new credential and returns it. Its approved
#: reveal answers with :data:`SECRET_VALUE` in ``secret_id``.
MINT_OP = "vault.auth.approle.generate_secret_id"
#: A path whose read fails: the approved run ends with an error and no value.
BROKEN_PATH = "app/broken"


class _VaultStub(Connector):
    product = "vault"
    version = "1.x"
    impl_id = "vault"

    async def fingerprint(self, target: Any, operator: Any = None) -> FingerprintResult:  # type: ignore[override]
        raise NotImplementedError

    async def probe(self, target: Any) -> ProbeResult:  # type: ignore[override]
        raise NotImplementedError

    async def execute(self, target: Any, op_id: str, params: dict[str, Any]) -> OperationResult:  # type: ignore[override]
        raise NotImplementedError


async def _read_handler(operator: Operator, target: Any, params: dict[str, Any]) -> dict[str, Any]:
    if params.get("path") == BROKEN_PATH:
        raise RuntimeError("the read failed")
    return {"data": {"password": SECRET_VALUE, "username": "app"}, "version": 1}


async def _plain_handler(operator: Operator, target: Any, params: dict[str, Any]) -> dict[str, Any]:
    return {"name": "app", "password": SECRET_VALUE}


async def _mint_handler(operator: Operator, target: Any, params: dict[str, Any]) -> dict[str, Any]:
    return {"data": {"secret_id": SECRET_VALUE, "secret_id_accessor": "accessor-6273"}}


def _embedding_service() -> AsyncMock:
    service = AsyncMock()
    service.encode_one.return_value = [0.0] * EMBEDDING_DIMENSION
    service.encode.return_value = [[0.0] * EMBEDDING_DIMENSION]
    service.dimension = EMBEDDING_DIMENSION
    return service


async def register_plain_read() -> None:
    """Register :data:`PLAIN_OP`, which answers with :data:`SECRET_VALUE` in ``password``.

    Call after :func:`register_vault_read`, which registers the connector.
    """
    await register_typed_operation(
        product="vault",
        version="1.x",
        impl_id="vault",
        op_id=PLAIN_OP,
        handler=_plain_handler,
        summary="Read the app config.",
        description="Read the app config.",
        parameter_schema={"type": "object", "properties": {}, "additionalProperties": False},
        when_to_use=None,
        embedding_service=_embedding_service(),
    )


async def register_mint_op() -> None:
    """Register :data:`MINT_OP`, which answers with a new :data:`SECRET_VALUE`.

    Call after :func:`register_vault_read`, which registers the connector.
    """
    await register_typed_operation(
        product="vault",
        version="1.x",
        impl_id="vault",
        op_id=MINT_OP,
        handler=_mint_handler,
        summary="Make a new AppRole secret ID.",
        description="Make a new AppRole secret ID.",
        parameter_schema={"type": "object", "properties": {}, "additionalProperties": False},
        when_to_use=None,
        embedding_service=_embedding_service(),
    )


async def register_vault_read() -> None:
    """Register a ``vault.kv.read`` stand-in that answers with :data:`SECRET_VALUE`."""
    service = _embedding_service()
    register_connector_v2(product="vault", version="", impl_id="", cls=_VaultStub)
    await register_typed_operation(
        product="vault",
        version="1.x",
        impl_id="vault",
        op_id=READ_OP,
        handler=_read_handler,
        summary="Read a KV v2 secret.",
        description="Read a secret.",
        parameter_schema={
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
            "additionalProperties": False,
        },
        when_to_use=None,
        embedding_service=service,
    )


async def parked_reveal(requester: Operator, path: str | None = "app/db") -> UUID:
    """*requester* asks to see the value at *path*; the call waits for approval.

    With *path* ``None``, *requester* asks for a new credential of :data:`MINT_OP`.
    """
    parked = await dispatch(
        operator=requester,
        connector_id=CONNECTOR_ID,
        op_id=READ_OP if path is not None else MINT_OP,
        target=None,
        params={"path": path, "reveal_secret": True}
        if path is not None
        else {"reveal_secret": True},
    )
    assert parked.status == "awaiting_approval", parked.error
    return UUID(parked.extras["approval_request_id"])


async def approved_reveal(requester: Operator, path: str | None = "app/db") -> UUID:
    """*requester*'s reveal waits; a tenant admin approves it. Nothing runs.

    An admin may approve also a dangerous change, such as :data:`MINT_OP`.
    """
    request_id = await parked_reveal(requester, path)
    reviewer = requester.model_copy(
        update={"sub": "reviewer-6273", "tenant_role": TenantRole.TENANT_ADMIN}
    )
    async with get_sessionmaker()() as session:
        approved = await approve_request(session, request_id, operator=reviewer)
        await session.commit()
    answer = await resume_dispatch_after_approval(operator=reviewer, request=approved, params=None)
    assert answer.status == WAITING_FOR_REQUESTER, answer
    assert SECRET_VALUE not in answer.model_dump_json()
    return request_id


async def run_without_value(monkeypatch: pytest.MonkeyPatch, requester: Operator, how: str) -> UUID:
    """An approved reveal whose run gives no value: it *how* = ``failed`` or ``refused``.

    ``failed``: the read runs and raises. ``refused``: the saved target
    changed after the approval, so the run is refused and nothing runs.
    """
    if how == "failed":
        return await approved_reveal(requester, BROKEN_PATH)
    request_id = await approved_reveal(requester)

    async def _target_changed(
        operator: Operator, request: ApprovalRequest
    ) -> tuple[Any, OperationResult | None]:
        return None, OperationResult(
            status="denied",
            op_id=request.op_id,
            error="target_changed_since_approval",
            duration_ms=0.0,
        )

    monkeypatch.setattr(approval_queue, "refuse_resume_if_target_changed", _target_changed)
    return request_id
