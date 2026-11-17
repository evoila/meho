# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Test helpers: the targets a settings import sends for approval.

A settings import writes no target itself: each target it creates or
changes waits for a person's approval as a ``targets.register`` or
``targets.update`` call. These helpers register the two operations and
approve every waiting request of an import answer as a second person.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from typing import Any
from unittest.mock import AsyncMock

import pytest_asyncio

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors.targets.ops import register_targets_registry_operations
from meho_backplane.connectors.targets.update import register_targets_update_operation
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.operations import reset_dispatcher_caches
from meho_backplane.operations.approval_queue import (
    approve_request,
    resume_dispatch_after_approval,
)


@pytest_asyncio.fixture
async def targets_ops_registered() -> AsyncIterator[None]:
    """Register ``targets.register`` and ``targets.update`` for the test."""
    embedding = AsyncMock()
    embedding.encode_one.return_value = [0.1] * 384
    embedding.encode.return_value = [[0.1] * 384]
    embedding.dimension = 384
    reset_dispatcher_caches()
    await register_targets_registry_operations(embedding_service=embedding)
    await register_targets_update_operation(embedding_service=embedding)
    yield
    reset_dispatcher_caches()


def _approver(tenant_id: uuid.UUID) -> Operator:
    return Operator(
        sub="approver-person",
        name=None,
        email=None,
        raw_jwt="fixture-jwt-not-real",
        tenant_id=tenant_id,
        tenant_role=TenantRole.TENANT_ADMIN,
        web_session=True,
    )


async def approve_waiting(body: dict[str, Any], tenant_id: uuid.UUID) -> list[Any]:
    """Approve each request in ``waiting_for_approval`` and run it; return the results."""
    approver = _approver(tenant_id)
    results = []
    for waiting in body["waiting_for_approval"]:
        async with get_sessionmaker()() as session:
            request = await approve_request(
                session, uuid.UUID(waiting["approval_request_id"]), operator=approver
            )
            await session.commit()
        result = await resume_dispatch_after_approval(operator=approver, request=request)
        assert result.status == "ok", result
        results.append(result)
    return results
