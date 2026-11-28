# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""A call that writes or makes a credential waits for a person, whatever its connector sets.

* An operation of class ``credential_write`` or ``credential_mint`` that a
  connector registers at ``safe`` or ``caution`` without the approval flag
  gets the flag when it is looked up. A person's call waits for approval,
  with a token and in the web console. One approval request is stored and
  the handler does not run. A call of an operation a connector marked as
  credential capable waits the same way.
* Every built-in operation that writes or makes a credential, or carries
  the credential mark, has the approval flag after the lookup.
* The ingest floor turns the flag on for an ingested operation whose op id
  is declared ``credential_write``. The risk level stays as it is.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from typing import Any
from unittest.mock import AsyncMock
from uuid import UUID

import pytest
from sqlalchemy import select

from meho_backplane.auth.operator import Operator, PrincipalKind, TenantRole
from meho_backplane.broadcast.events import resolve_op_class
from meho_backplane.connectors.registry import clear_registry, register_connector_v2
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import ApprovalRequest, EndpointDescriptor
from meho_backplane.op_classes import (
    _CREDENTIAL_CAPABLE,
    _DECLARED,
    declare_credential_capable,
    declare_op_classes,
    is_credential_capable,
)
from meho_backplane.operations import dispatch, register_typed_operation, reset_dispatcher_caches
from meho_backplane.operations._lookup import lookup_descriptor
from meho_backplane.operations.ingest.safety_floors import apply_safety_floor, has_safety_floor
from meho_backplane.operations.ingest.schemas import EndpointDescriptorProto
from meho_backplane.operations.op_policy import _with_floors, effective_policy
from meho_backplane.settings import get_settings
from tests._composite_wait_audit_helpers import NoOpVaultConnector
from tests.test_credential_calls_always_wait import _register_all

pytestmark = pytest.mark.asyncio

_TENANT = UUID("00000000-0000-0000-0000-00000000c4e1")
_CONNECTOR_ID = "vault-1.x"
_WRITE = "vault.standin.secret.write"
_MINT = "vault.standin.token.make"
_MARKED = "vault.standin.raw.write"
_INGESTED_WRITE = "PUT:/v1/example/standin-keys/{id}"
_CHANGE_CLASSES = frozenset({"credential_write", "credential_mint"})

ran: list[str] = []


async def standin_handler(target: Any, params: dict[str, Any]) -> dict[str, Any]:
    ran.append("ran")
    return {"done": True}


@pytest.fixture(autouse=True)
def _state(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    reset_dispatcher_caches()
    clear_registry()
    ran.clear()
    declare_op_classes(credential_write=(_WRITE, _INGESTED_WRITE), credential_mint=(_MINT,))
    declare_credential_capable((_MARKED,))
    yield
    for op_id in (_WRITE, _MINT, _INGESTED_WRITE):
        _DECLARED.pop(op_id, None)
    _CREDENTIAL_CAPABLE.discard(_MARKED)
    reset_dispatcher_caches()
    clear_registry()
    get_settings.cache_clear()


@pytest.fixture
async def embedding() -> AsyncIterator[AsyncMock]:
    service = AsyncMock()
    service.encode_one.return_value = [0.1] * 384
    service.encode.return_value = [[0.1] * 384]
    service.dimension = 384
    yield service


def _person(*, web_session: bool) -> Operator:
    return Operator(
        sub="op-person",
        name="A Person",
        email=None,
        raw_jwt="<test-raw-jwt>",
        tenant_id=_TENANT,
        tenant_role=TenantRole.OPERATOR,
        principal_kind=PrincipalKind.USER,
        web_session=web_session,
    )


async def _register_standin(embedding: AsyncMock, op_id: str, level: str) -> None:
    register_connector_v2(product="vault", version="", impl_id="", cls=NoOpVaultConnector)
    await register_typed_operation(
        product="vault",
        version="1.x",
        impl_id="vault",
        op_id=op_id,
        handler=standin_handler,
        summary="A stand-in credential call.",
        description="A stand-in credential call.",
        parameter_schema={"type": "object"},
        when_to_use=None,
        embedding_service=embedding,
        safety_level=level,
        requires_approval=False,
    )


async def _requests(op_id: str) -> list[ApprovalRequest]:
    async with get_sessionmaker()() as s:
        stmt = select(ApprovalRequest).where(ApprovalRequest.op_id == op_id)
        return list((await s.execute(stmt)).scalars().all())


@pytest.mark.parametrize("op_id", [_WRITE, _MINT], ids=["credential_write", "credential_mint"])
@pytest.mark.parametrize("level", ["safe", "caution"])
@pytest.mark.parametrize("web_session", [False, True], ids=["token", "web-console"])
async def test_a_persons_credential_write_waits_without_the_connector_flag(
    embedding: AsyncMock, op_id: str, level: str, web_session: bool
) -> None:
    await _register_standin(embedding, op_id, level)

    descriptor = await lookup_descriptor(
        tenant_id=_TENANT, product="vault", version="1.x", impl_id="vault", op_id=op_id
    )
    assert descriptor is not None
    assert descriptor.requires_approval is True
    assert descriptor.safety_level == level

    result = await dispatch(
        operator=_person(web_session=web_session),
        connector_id=_CONNECTOR_ID,
        op_id=op_id,
        target=None,
        params={},
    )

    assert result.status == "awaiting_approval", result.error
    [request] = await _requests(op_id)
    assert str(request.id) == result.extras["approval_request_id"]
    assert ran == []


@pytest.mark.parametrize("web_session", [False, True], ids=["token", "web-console"])
async def test_a_persons_call_of_a_marked_operation_waits_without_the_connector_flag(
    embedding: AsyncMock, web_session: bool
) -> None:
    await _register_standin(embedding, _MARKED, "safe")

    result = await dispatch(
        operator=_person(web_session=web_session),
        connector_id=_CONNECTOR_ID,
        op_id=_MARKED,
        target=None,
        params={},
    )

    assert result.status == "awaiting_approval", result.error
    [request] = await _requests(_MARKED)
    assert str(request.id) == result.extras["approval_request_id"]
    assert ran == []


async def test_every_built_in_credential_write_has_the_approval_flag_after_the_lookup() -> None:
    rows = await _register_all()
    checked = [
        row
        for row in rows
        if resolve_op_class(row.op_id, row.op_class) in _CHANGE_CLASSES
        or is_credential_capable(row.op_id)
    ]
    assert checked, "no built-in operation writes or makes a credential"
    missing = []
    for row in checked:
        descriptor = await lookup_descriptor(
            tenant_id=_TENANT,
            product=row.product,
            version=row.version,
            impl_id=row.impl_id,
            op_id=row.op_id,
        )
        assert descriptor is not None, row.op_id
        if not descriptor.requires_approval:
            missing.append(row.op_id)
    assert missing == []


def _row(op_id: str, *, requires_approval: bool) -> EndpointDescriptor:
    return EndpointDescriptor(
        product="vault",
        version="1.x",
        impl_id="vault",
        op_id=op_id,
        source_kind="typed",
        safety_level="safe",
        requires_approval=requires_approval,
        parameter_schema={},
        tenant_id=None,
    )


@pytest.mark.parametrize("op_id", [_WRITE, _MINT])
async def test_the_lookup_floor_turns_approval_on_and_keeps_the_level(op_id: str) -> None:
    row = _row(op_id, requires_approval=False)

    floored = _with_floors(row, effective_policy("safe", False, []))

    assert floored.requires_approval is True
    assert floored.safety_level == "safe"


async def test_the_lookup_floor_leaves_a_plain_write_as_it_is() -> None:
    row = _row("vault.standin.config.write", requires_approval=False)

    floored = _with_floors(row, effective_policy("safe", False, []))

    assert floored.requires_approval is False


def _proto(op_id: str) -> EndpointDescriptorProto:
    return EndpointDescriptorProto(
        op_id=op_id,
        method="PUT",
        path="/v1/example/standin-keys/{id}",
        safety_level="safe",
        requires_approval=False,
        parameter_schema={},
    )


async def test_the_ingest_floor_turns_approval_on_for_a_declared_credential_write() -> None:
    proto = _proto(_INGESTED_WRITE)

    floored = apply_safety_floor(product="example", version="1.0", impl_id="rest", proto=proto)

    assert floored.requires_approval is True
    assert floored.safety_level == "safe"
    assert has_safety_floor(product="example", version="1.0", impl_id="rest", proto=proto)


async def test_the_ingest_floor_leaves_a_plain_write_as_it_is() -> None:
    proto = _proto("PUT:/v1/example/settings/{id}")

    floored = apply_safety_floor(product="example", version="1.0", impl_id="rest", proto=proto)

    assert floored is proto
