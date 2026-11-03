# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Two removals of an operation's overrides cannot both end approval (#4711).

The unit suite runs on SQLite, which has one writer at a time, so it cannot
hold two transactions open at once. These tests do, against the real
PostgreSQL container.

The setup is the race from the review: the built-in operation requires
approval, the tenant's copy does not, and both scopes hold an override
that requires approval. Each removal on its own is harmless, because the
other override still requires approval. Both together would leave the
tenant without approval, and neither asked for a reason or a fresh
sign-in. The per-operation lock in ``op_policy`` makes the second removal
wait for the first and then read what it wrote, so it is refused.
"""

from __future__ import annotations

import asyncio
import uuid

import pytest
from sqlalchemy import select

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import EndpointDescriptor, OperationPolicyOverride
from meho_backplane.operations._lookup import lookup_descriptor
from meho_backplane.operations.ingest import ReviewService
from meho_backplane.operations.op_policy import OpKey, OpPolicyEditError, remove_override
from tests.integration.conftest import DOCKER_AVAILABLE, SKIP_REASON

# Matches the tenant row the ``pg_engine`` conftest fixture seeds.
TENANT_A_ID = uuid.UUID("11111111-1111-1111-1111-111111111111")
_OP = "pets.update"
_VERSION = "1.0"

_skip_no_docker = pytest.mark.skipif(not DOCKER_AVAILABLE, reason=SKIP_REASON)


def _descriptor(
    impl_id: str, *, tenant_id: uuid.UUID | None, requires_approval: bool
) -> EndpointDescriptor:
    return EndpointDescriptor(
        tenant_id=tenant_id,
        product=impl_id,
        version=_VERSION,
        impl_id=impl_id,
        op_id=_OP,
        source_kind="ingested",
        method="POST",
        path="/pets",
        parameter_schema={"type": "object", "properties": {}},
        safety_level="caution",
        requires_approval=requires_approval,
        is_enabled=True,
    )


def _override(impl_id: str, *, tenant_id: uuid.UUID | None) -> OperationPolicyOverride:
    return OperationPolicyOverride(
        tenant_id=tenant_id,
        product=impl_id,
        version=_VERSION,
        impl_id=impl_id,
        op_id=_OP,
        requires_approval=True,
        set_by_sub="op-admin",
    )


async def _seed_race() -> tuple[str, OpKey]:
    """Built-in op requires approval, tenant copy does not, both scopes require it by override.

    ``endpoint_descriptor`` and ``operation_policy_override`` are not
    truncated between tests, so each test uses its own connector name.
    """
    impl_id = f"polrace{uuid.uuid4().hex[:8]}"
    async with get_sessionmaker()() as session:
        session.add_all(
            [
                _descriptor(impl_id, tenant_id=None, requires_approval=True),
                _descriptor(impl_id, tenant_id=TENANT_A_ID, requires_approval=False),
                _override(impl_id, tenant_id=None),
                _override(impl_id, tenant_id=TENANT_A_ID),
            ]
        )
        await session.commit()
    return f"{impl_id}-{_VERSION}", OpKey(impl_id, _VERSION, impl_id, _OP)


def _platform_admin() -> Operator:
    return Operator(
        sub="op-admin",
        name="Admin",
        email=None,
        raw_jwt="header.payload.signature",
        tenant_id=uuid.uuid4(),
        tenant_role=TenantRole.TENANT_ADMIN,
        platform_admin=True,
    )


async def _tenant_requires_approval(key: OpKey) -> bool:
    row = await lookup_descriptor(
        tenant_id=TENANT_A_ID,
        product=key.product,
        version=key.version,
        impl_id=key.impl_id,
        op_id=key.op_id,
    )
    assert row is not None
    return row.requires_approval


async def _override_scopes(key: OpKey) -> list[uuid.UUID | None]:
    async with get_sessionmaker()() as session:
        rows = (
            await session.execute(
                select(OperationPolicyOverride.tenant_id).where(
                    OperationPolicyOverride.impl_id == key.impl_id,
                    OperationPolicyOverride.op_id == key.op_id,
                )
            )
        ).scalars()
        return list(rows)


async def _remove_without_reason(connector_id: str, scope: uuid.UUID | None) -> None:
    await ReviewService(_platform_admin()).remove_op_override(
        connector_id, _OP, override_tenant_id=scope, reason=None, person_present=False
    )


@_skip_no_docker
@pytest.mark.parametrize(
    ("first", "second"),
    [(None, TENANT_A_ID), (TENANT_A_ID, None)],
    ids=["install-then-tenant", "tenant-then-install"],
)
async def test_second_removal_waits_for_the_first_then_is_refused(
    pg_engine: None, first: uuid.UUID | None, second: uuid.UUID | None
) -> None:
    connector_id, key = await _seed_race()
    session_1 = get_sessionmaker()()
    try:
        # The first removal passes: the other scope's override still requires
        # approval. It holds the per-operation lock until it commits.
        await remove_override(
            session_1,
            key=key,
            scope_tenant_id=first,
            operator=_platform_admin(),
            reason=None,
            person_present=False,
            max_age_seconds=300,
        )

        second_task = asyncio.create_task(_remove_without_reason(connector_id, second))
        await asyncio.sleep(0.5)
        assert not second_task.done(), "the second removal did not wait for the first"

        await session_1.commit()
    finally:
        await session_1.close()

    # Now the second removal reads the committed state: it is the last
    # override that requires approval, so ending it needs a reason.
    with pytest.raises(OpPolicyEditError) as caught:
        await asyncio.wait_for(second_task, timeout=10)
    assert caught.value.code == "approval_off_needs_reason"

    assert await _override_scopes(key) == [second]
    assert await _tenant_requires_approval(key) is True


@_skip_no_docker
async def test_two_removals_at_once_let_only_one_through(pg_engine: None) -> None:
    connector_id, key = await _seed_race()

    results = await asyncio.gather(
        _remove_without_reason(connector_id, None),
        _remove_without_reason(connector_id, TENANT_A_ID),
        return_exceptions=True,
    )

    refused = [r for r in results if isinstance(r, OpPolicyEditError)]
    assert len(refused) == 1, results
    assert refused[0].code == "approval_off_needs_reason"
    assert len(await _override_scopes(key)) == 1
    assert await _tenant_requires_approval(key) is True
