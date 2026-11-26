# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The row keeps the code's own risk level and approval flag, and the plain mark.

What these tests prove:

* After :func:`~meho_backplane.operations.op_policy.apply_effective_policy`
  and :func:`~meho_backplane.operations.op_policy.apply_step_policy`,
  ``row.code_safety_level`` and ``row.code_requires_approval`` hold the
  code's values, also when an admin's edit raised the level or turned
  approval off.
* :func:`~meho_backplane.operations.op_policy.has_plain_mark` is true for a
  registered operation whose only reason to wait is the code's mark, and
  false for an admin's "approval on", a credential call, a caller address,
  a change of rights, "approve each call" and a delete sign. The cases come
  from the operations MEHO registers at start-up.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Iterator
from typing import Any
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from meho_backplane.connectors.registry import clear_registry
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import EndpointDescriptor, OperationPolicyOverride
from meho_backplane.op_classes import needs_per_call_approval
from meho_backplane.operations import register_typed_operation, reset_dispatcher_caches
from meho_backplane.operations._lookup import lookup_descriptor
from meho_backplane.operations.caller_address import has_caller_address
from meho_backplane.operations.grant_approval import is_rights_change_op
from meho_backplane.operations.op_policy import apply_step_policy, has_plain_mark
from meho_backplane.operations.service_grants import (
    delete_grant_reason,
    descriptor_credential_reason,
)
from meho_backplane.operations.typed_register import run_typed_op_registrars
from meho_backplane.settings import get_settings

_TENANT = uuid.UUID("00000000-0000-0000-0000-00000000c0de")
_KEY = {"product": "fixturepets", "version": "1.x", "impl_id": "fixturepets"}


@pytest.fixture(autouse=True)
def _state(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    reset_dispatcher_caches()
    clear_registry()
    yield
    reset_dispatcher_caches()
    clear_registry()
    get_settings.cache_clear()


def _embedding() -> AsyncMock:
    service = AsyncMock()
    service.encode_one.return_value = [0.1] * 384
    service.encode.return_value = [[0.1] * 384]
    service.dimension = 384
    return service


async def _handler(target: Any, params: dict[str, Any]) -> dict[str, Any]:
    return {}


async def _register(op_id: str, *, safety_level: Any, requires_approval: bool) -> None:
    await register_typed_operation(
        **_KEY,
        op_id=op_id,
        handler=_handler,
        summary="Change a pet.",
        description="Change a pet.",
        parameter_schema={"type": "object"},
        when_to_use=None,
        safety_level=safety_level,
        requires_approval=requires_approval,
        embedding_service=_embedding(),
    )


async def _override(
    op_id: str, *, safety_level: str | None, requires_approval: bool | None
) -> None:
    async with get_sessionmaker()() as session, session.begin():
        session.add(
            OperationPolicyOverride(
                tenant_id=_TENANT,
                **_KEY,
                op_id=op_id,
                safety_level=safety_level,
                requires_approval=requires_approval,
                reason="fixture: admin edit",
                set_by_sub="fixture-admin",
            )
        )


async def _looked_up(op_id: str) -> EndpointDescriptor:
    row = await lookup_descriptor(tenant_id=_TENANT, op_id=op_id, **_KEY)
    assert row is not None
    return row


@pytest.mark.asyncio
async def test_a_raise_keeps_the_code_values_next_to_the_effective_ones() -> None:
    op_id = "fixturepets.pet.update"
    await _register(op_id, safety_level="caution", requires_approval=False)
    await _override(op_id, safety_level="dangerous", requires_approval=True)

    row = await _looked_up(op_id)

    assert (row.safety_level, row.requires_approval) == ("dangerous", True)
    assert (row.code_safety_level, row.code_requires_approval) == ("caution", False)
    assert has_plain_mark(row) is False


@pytest.mark.asyncio
async def test_approval_off_keeps_the_code_mark() -> None:
    op_id = "fixturepets.pet.rename"
    await _register(op_id, safety_level="caution", requires_approval=True)
    await _override(op_id, safety_level=None, requires_approval=False)

    row = await _looked_up(op_id)

    assert (row.safety_level, row.requires_approval) == ("caution", False)
    assert (row.code_safety_level, row.code_requires_approval) == ("caution", True)
    assert has_plain_mark(row) is True


@pytest.mark.asyncio
async def test_a_step_keeps_the_code_values_after_a_raise() -> None:
    op_id = "fixturepets.pet.move"
    await _override(op_id, safety_level="dangerous", requires_approval=True)
    step = EndpointDescriptor(
        **_KEY,
        op_id=op_id,
        source_kind="composite",
        safety_level="caution",
        requires_approval=False,
        parameter_schema={},
    )

    await apply_step_policy(tenant_id=_TENANT, step=step)

    assert (step.safety_level, step.requires_approval) == ("dangerous", True)
    assert (step.code_safety_level, step.code_requires_approval) == ("caution", False)


# ---------------------------------------------------------------------------
# has_plain_mark on the operations MEHO registers at start-up
# ---------------------------------------------------------------------------


def _delete_sign(row: EndpointDescriptor) -> bool:
    patterns = get_settings().service_grant_delete_shaped_patterns
    return delete_grant_reason(row.op_id, patterns, row) is not None


_REASONS: dict[str, Callable[[EndpointDescriptor], bool]] = {
    "credential call": lambda row: descriptor_credential_reason(row) is not None,
    "caller address": lambda row: has_caller_address(row.parameter_schema),
    "change of rights": lambda row: is_rights_change_op(row.op_id),
    "approve each call": lambda row: needs_per_call_approval(row.op_id),
    "delete sign": _delete_sign,
}


async def _registered() -> list[EndpointDescriptor]:
    await run_typed_op_registrars(embedding_service=_embedding())
    async with get_sessionmaker()() as session:
        stmt = select(EndpointDescriptor).where(
            EndpointDescriptor.tenant_id.is_(None),
            EndpointDescriptor.source_kind.in_(("typed", "composite")),
        )
        return list((await session.execute(stmt)).scalars().all())


@pytest.mark.asyncio
async def test_has_plain_mark_on_registered_operations() -> None:
    rows = [row for row in await _registered() if row.requires_approval]

    for reason, applies in _REASONS.items():
        with_reason = [row for row in rows if applies(row)]
        assert with_reason, f"no registered operation with a {reason}"
        marked = [row.op_id for row in with_reason if has_plain_mark(row)]
        assert marked == [], f"{reason}: {marked}"

    plain = [row for row in rows if not any(applies(row) for applies in _REASONS.values())]
    assert plain, "no registered operation with only the plain mark"
    assert [row.op_id for row in plain if not has_plain_mark(row)] == []


@pytest.mark.asyncio
async def test_an_admin_approval_on_is_not_the_plain_mark() -> None:
    rows = await _registered()
    row = next(
        row
        for row in rows
        if not row.requires_approval
        and row.safety_level == "safe"
        and not any(applies(row) for applies in _REASONS.values())
    )
    async with get_sessionmaker()() as session, session.begin():
        session.add(
            OperationPolicyOverride(
                tenant_id=_TENANT,
                product=row.product,
                version=row.version,
                impl_id=row.impl_id,
                op_id=row.op_id,
                safety_level=None,
                requires_approval=True,
                reason="fixture: admin turns approval on",
                set_by_sub="fixture-admin",
            )
        )

    looked_up = await lookup_descriptor(
        tenant_id=_TENANT,
        product=row.product,
        version=row.version,
        impl_id=row.impl_id,
        op_id=row.op_id,
    )

    assert looked_up is not None
    assert looked_up.requires_approval is True
    assert looked_up.code_requires_approval is False
    assert has_plain_mark(looked_up) is False
