# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""An override does not turn off the approval of a credential write or mint.

The code of an operation of class ``credential_write`` or
``credential_mint`` turns approval on. An install or tenant override that
turns approval off leaves it on, as for a credential read. A person's own
call in the console waits for approval. The same override on a plain write
still runs the call at once, so the override itself works.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from typing import Any
from unittest.mock import AsyncMock

import pytest

from meho_backplane.connectors.registry import clear_registry
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import OperationPolicyOverride
from meho_backplane.op_classes import _DECLARED, declare_op_classes
from meho_backplane.operations import (
    dispatch,
    register_typed_operation,
    reset_dispatcher_caches,
)
from meho_backplane.operations._lookup import lookup_descriptor
from meho_backplane.operations.op_policy import effective_policy
from meho_backplane.settings import get_settings
from tests import _composite_wait_audit_helpers as h

_WRITE = "vault.fixture.keys.write"
_MINT = "vault.fixture.keys.make"
_PLAIN = "vault.fixture.notes.write"

ran: list[str] = []


@pytest.fixture(autouse=True)
def _state(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    reset_dispatcher_caches()
    clear_registry()
    declare_op_classes(credential_write=(_WRITE,), credential_mint=(_MINT,))
    ran.clear()
    try:
        yield
    finally:
        _DECLARED.pop(_WRITE, None)
        _DECLARED.pop(_MINT, None)
        reset_dispatcher_caches()
        clear_registry()
        get_settings.cache_clear()


async def _handler(target: Any, params: dict[str, Any]) -> dict[str, Any]:
    ran.append("ran")
    return {"done": True}


async def _register(op_id: str) -> None:
    embedding = AsyncMock()
    embedding.encode_one.return_value = h.EMBEDDING
    embedding.encode.return_value = [h.EMBEDDING]
    embedding.dimension = 384
    await h.register_child(embedding)
    await register_typed_operation(
        product="vault",
        version="1.x",
        impl_id="vault",
        op_id=op_id,
        handler=_handler,
        summary="Fixture write.",
        description="Fixture write.",
        parameter_schema={"type": "object"},
        when_to_use=None,
        safety_level="caution",
        requires_approval=True,
        embedding_service=embedding,
    )


async def _turn_approval_off(op_id: str, tenant_id: uuid.UUID | None) -> None:
    async with get_sessionmaker()() as session, session.begin():
        session.add(
            OperationPolicyOverride(
                tenant_id=tenant_id,
                product="vault",
                version="1.x",
                impl_id="vault",
                op_id=op_id,
                safety_level=None,
                requires_approval=False,
                reason="fixture: approval turned off",
                set_by_sub="fixture-admin",
            )
        )


async def _console_call(op_id: str) -> Any:
    person = h.operator(sub="person-own-call").model_copy(update={"web_session": True})
    return await dispatch(
        operator=person, connector_id=h.CONNECTOR_ID, op_id=op_id, target=None, params={}
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("op_id", [_WRITE, _MINT])
@pytest.mark.parametrize("scope", ["install", "tenant"])
async def test_an_override_does_not_turn_off_approval_of_a_credential_write_or_mint(
    op_id: str, scope: str
) -> None:
    await _register(op_id)
    await _turn_approval_off(op_id, None if scope == "install" else h.TENANT_ID)

    descriptor = await lookup_descriptor(
        tenant_id=h.TENANT_ID, product="vault", version="1.x", impl_id="vault", op_id=op_id
    )
    assert descriptor is not None
    assert descriptor.requires_approval is True

    result = await _console_call(op_id)

    assert result.status == "awaiting_approval", result.error
    assert ran == []


@pytest.mark.asyncio
@pytest.mark.parametrize("scope", ["install", "tenant"])
async def test_the_same_override_still_turns_off_approval_of_a_plain_write(scope: str) -> None:
    await _register(_PLAIN)
    await _turn_approval_off(_PLAIN, None if scope == "install" else h.TENANT_ID)

    result = await _console_call(_PLAIN)

    assert result.status == "ok", result.error
    assert ran == ["ran"]


def test_effective_policy_alone_still_follows_the_override() -> None:
    """The floor is applied after the overrides, not inside ``effective_policy``."""
    override = OperationPolicyOverride(requires_approval=False)
    assert effective_policy("caution", True, [override]).requires_approval is False
