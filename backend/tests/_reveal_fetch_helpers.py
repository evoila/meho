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

from meho_backplane.auth.operator import Operator
from meho_backplane.connectors.base import Connector
from meho_backplane.connectors.registry import register_connector_v2
from meho_backplane.connectors.schemas import FingerprintResult, OperationResult, ProbeResult
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.operations import dispatch, register_typed_operation
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
    return {"data": {"password": SECRET_VALUE, "username": "app"}, "version": 1}


async def register_vault_read() -> None:
    """Register a ``vault.kv.read`` stand-in that answers with :data:`SECRET_VALUE`."""
    service = AsyncMock()
    service.encode_one.return_value = [0.0] * EMBEDDING_DIMENSION
    service.encode.return_value = [[0.0] * EMBEDDING_DIMENSION]
    service.dimension = EMBEDDING_DIMENSION
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


async def parked_reveal(requester: Operator) -> UUID:
    """*requester* asks to see the value; the call waits for approval."""
    parked = await dispatch(
        operator=requester,
        connector_id=CONNECTOR_ID,
        op_id=READ_OP,
        target=None,
        params={"path": "app/db", "reveal_secret": True},
    )
    assert parked.status == "awaiting_approval", parked.error
    return UUID(parked.extras["approval_request_id"])


async def approved_reveal(requester: Operator) -> UUID:
    """*requester*'s reveal waits; another person of the tenant approves it. Nothing runs."""
    request_id = await parked_reveal(requester)
    reviewer = requester.model_copy(update={"sub": "reviewer-6273"})
    async with get_sessionmaker()() as session:
        approved = await approve_request(session, request_id, operator=reviewer)
        await session.commit()
    answer = await resume_dispatch_after_approval(operator=reviewer, request=approved, params=None)
    assert answer.status == WAITING_FOR_REQUESTER, answer
    assert SECRET_VALUE not in answer.model_dump_json()
    return request_id
