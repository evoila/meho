# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""A connector's safety floor sees the full answer schema and can set the op class.

* The stored answer schema holds the shared schemas it uses, under
  ``components.schemas``, so a floor can look inside them.
* A floor sets ``op_class`` on an ingested operation. Ingest stores it on
  the row, so a second process that has no floor and no op class list
  still applies it: the call waits for approval, and the audit row and the
  approval request carry the class.
* A floor with two arguments (version, operation) works as before.
* An operation no floor touches keeps its stored risk level, approval flag
  and op class.
* Ingest refuses a class that is not allowed, and names the operation.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator
from typing import Any
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from meho_backplane.auth.operator import Operator, PrincipalKind, TenantRole
from meho_backplane.broadcast.events import resolve_op_class, resolve_operation_op_class
from meho_backplane.connectors.base import Connector
from meho_backplane.connectors.registry import clear_registry, register_connector_v2
from meho_backplane.connectors.schemas import FingerprintResult, OperationResult, ProbeResult
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import ApprovalRequest, EndpointDescriptor
from meho_backplane.operations import dispatch, reset_dispatcher_caches
from meho_backplane.operations._audit import _build_audit_payload, descriptor_op_class
from meho_backplane.operations._lookup import lookup_descriptor
from meho_backplane.operations.ingest import parse_openapi, register_ingested_operations
from meho_backplane.operations.ingest.exceptions import InvalidOpClassError
from meho_backplane.operations.ingest.safety_floors import (
    _FLOORS,
    apply_safety_floor,
    register_ingest_safety_floor,
)
from meho_backplane.operations.ingest.schemas import EndpointDescriptorProto
from meho_backplane.settings import get_settings

_PRODUCT = "clientsfixture"
_VERSION = "1.0"
_IMPL_ID = "clientsfixture-rest"

_SPEC = """
openapi: 3.0.3
info: {title: clients fixture, version: '1.0'}
paths:
  /clients:
    get:
      operationId: clients.list
      responses:
        '200':
          description: ok
          content:
            application/json:
              schema:
                type: array
                items: {$ref: '#/components/schemas/Client'}
  /health:
    get:
      operationId: health
      responses:
        '200':
          description: ok
          content:
            application/json:
              schema:
                type: object
                properties: {status: {type: string}}
  /broken:
    get:
      operationId: broken
      responses:
        '200':
          description: ok
          content:
            application/json:
              schema:
                type: object
                properties:
                  thing: {$ref: '#/components/schemas/Missing'}
  /mixed:
    get:
      operationId: mixed
      responses:
        '200':
          description: ok
          content:
            application/json:
              schema:
                type: object
                properties:
                  client: {$ref: '#/components/schemas/Client'}
                  thing: {$ref: '#/components/schemas/Missing'}
components:
  schemas:
    Client:
      type: object
      properties:
        name: {type: string}
        owner: {$ref: '#/components/schemas/Owner'}
    Owner:
      type: object
      properties:
        email: {type: string}
        password: {type: string}
"""

_CLIENTS = "GET:/clients"
_HEALTH = "GET:/health"
_BROKEN = "GET:/broken"
_MIXED = "GET:/mixed"


@pytest.fixture(autouse=True)
def _required_settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
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


@pytest.fixture
def stub_embedding_service() -> AsyncMock:
    service = AsyncMock()
    service.encode_one.return_value = [0.25] * 384
    service.encode.return_value = [[0.25] * 384]
    service.dimension = 384
    return service


def _ops() -> dict[str, EndpointDescriptorProto]:
    rows = parse_openapi("file:///clients-fixture.yaml", content=_SPEC)
    return {row.op_id: row for row in rows}


def _password_names(node: object) -> int:
    """Count the ``password`` property names anywhere inside *node*."""
    if isinstance(node, dict):
        hits = 1 if "password" in node.get("properties", {}) else 0
        return hits + sum(_password_names(value) for value in node.values())
    if isinstance(node, list):
        return sum(_password_names(value) for value in node)
    return 0


def _credential_floor(version: str, proto: EndpointDescriptorProto) -> EndpointDescriptorProto:
    """A floor that reads the answer and marks an answer with a password field."""
    if _password_names(proto.response_schema) == 0:
        return proto
    return proto.model_copy(update={"op_class": "credential_read"})


async def _register(service: AsyncMock) -> None:
    await register_ingested_operations(
        product=_PRODUCT,
        version=_VERSION,
        impl_id=_IMPL_ID,
        spec_source="clients-fixture.yaml",
        operations=list(_ops().values()),
        embedding_service=service,
    )


async def _row(op_id: str) -> EndpointDescriptor:
    async with get_sessionmaker()() as session:
        return (
            await session.execute(
                select(EndpointDescriptor).where(
                    EndpointDescriptor.product == _PRODUCT, EndpointDescriptor.op_id == op_id
                )
            )
        ).scalar_one()


# -- Part 1: the answer schema ------------------------------------------------


def test_answer_schema_holds_the_shared_schemas_it_uses() -> None:
    schema = _ops()[_CLIENTS].response_schema

    assert schema is not None
    assert schema["items"] == {"$ref": "#/components/schemas/Client"}
    assert sorted(schema["components"]["schemas"]) == ["Client", "Owner"]  # type: ignore[index,call-overload]
    assert _password_names(schema) == 1
    json.dumps(schema)


def test_answer_schema_without_refs_gains_no_components_key() -> None:
    schema = _ops()[_HEALTH].response_schema

    assert schema == {"type": "object", "properties": {"status": {"type": "string"}}}


def test_answer_schema_with_a_missing_shared_schema_does_not_fail_ingest() -> None:
    schema = _ops()[_BROKEN].response_schema

    assert schema == {
        "type": "object",
        "properties": {"thing": {"$ref": "#/components/schemas/Missing"}},
    }


def test_answer_schema_keeps_the_shared_schemas_next_to_a_missing_one() -> None:
    """One missing shared schema does not hide the others from the floor."""
    register_ingest_safety_floor(product=_PRODUCT, impl_id=_IMPL_ID, floor=_credential_floor)
    proto = _ops()[_MIXED]
    schema = proto.response_schema

    assert schema is not None
    assert schema["properties"]["thing"] == {"$ref": "#/components/schemas/Missing"}  # type: ignore[index,call-overload]
    assert sorted(schema["components"]["schemas"]) == ["Client", "Owner"]  # type: ignore[index,call-overload]
    assert _password_names(schema) == 1
    floored = apply_safety_floor(product=_PRODUCT, version=_VERSION, impl_id=_IMPL_ID, proto=proto)
    assert floored.op_class == "credential_read"


def test_floor_sees_the_password_field_and_sets_the_class() -> None:
    register_ingest_safety_floor(product=_PRODUCT, impl_id=_IMPL_ID, floor=_credential_floor)
    ops = _ops()

    result = apply_safety_floor(
        product=_PRODUCT, version=_VERSION, impl_id=_IMPL_ID, proto=ops[_CLIENTS]
    )

    assert result.op_class == "credential_read"
    assert (result.safety_level, result.requires_approval) == ("caution", True)
    other = apply_safety_floor(
        product=_PRODUCT, version=_VERSION, impl_id=_IMPL_ID, proto=ops[_HEALTH]
    )
    assert other is ops[_HEALTH]


# -- Part 2: the class is stored and used by a second process ------------------


@pytest.mark.asyncio
async def test_ingest_stores_the_class_on_the_row(stub_embedding_service: AsyncMock) -> None:
    register_ingest_safety_floor(product=_PRODUCT, impl_id=_IMPL_ID, floor=_credential_floor)

    await _register(stub_embedding_service)

    clients = await _row(_CLIENTS)
    assert clients.op_class == "credential_read"
    assert (clients.safety_level, clients.requires_approval) == ("caution", True)
    health = await _row(_HEALTH)
    assert health.op_class is None
    assert (health.safety_level, health.requires_approval) == ("safe", False)


class _Connector(Connector):
    product = _PRODUCT
    version = _VERSION
    impl_id = _IMPL_ID

    async def fingerprint(self, target: Any, operator: Any = None) -> FingerprintResult:  # type: ignore[override]
        raise NotImplementedError

    async def probe(self, target: Any) -> ProbeResult:  # type: ignore[override]
        raise NotImplementedError

    async def execute(self, target: Any, op_id: str, params: dict[str, Any]) -> OperationResult:  # type: ignore[override]
        raise AssertionError("the call must wait for approval and never run")


class _Target:
    def __init__(self) -> None:
        self.id = uuid.uuid4()
        self.name = "clients-target"
        self.product = _PRODUCT
        self.fingerprint = type("Fingerprint", (), {"version": _VERSION})()
        self.preferred_impl_id: str | None = None


def _operator() -> Operator:
    return Operator(
        sub=f"sub-{uuid.uuid4()}",
        raw_jwt="header.payload.signature",
        tenant_id=uuid.uuid4(),
        tenant_role=TenantRole.OPERATOR,
        principal_kind=PrincipalKind.USER,
    )


@pytest.mark.asyncio
async def test_second_process_applies_the_stored_class(
    stub_embedding_service: AsyncMock,
) -> None:
    """Ingest with a floor, then start fresh: no floor, no op class list."""
    register_ingest_safety_floor(product=_PRODUCT, impl_id=_IMPL_ID, floor=_credential_floor)
    await _register(stub_embedding_service)
    async with get_sessionmaker()() as session, session.begin():
        row = (
            await session.execute(
                select(EndpointDescriptor).where(EndpointDescriptor.op_id == _CLIENTS)
            )
        ).scalar_one()
        row.is_enabled = True
        # A row as an older ingest stored it: the class is there, the level is low.
        row.safety_level = "safe"
        row.requires_approval = False

    _FLOORS.clear()
    reset_dispatcher_caches()
    register_connector_v2(product=_PRODUCT, version="", impl_id="", cls=_Connector)
    operator = _operator()

    looked_up = await lookup_descriptor(
        tenant_id=operator.tenant_id,
        product=_PRODUCT,
        version=_VERSION,
        impl_id=_IMPL_ID,
        op_id=_CLIENTS,
    )
    assert looked_up is not None
    assert (looked_up.safety_level, looked_up.requires_approval) == ("caution", True)
    assert descriptor_op_class(looked_up) == "credential_read"

    result = await dispatch(
        operator=operator,
        connector_id=f"{_IMPL_ID}-{_VERSION}",
        op_id=_CLIENTS,
        target=_Target(),
        params={},
    )

    assert result.status == "awaiting_approval", (result.status, result.error, dict(result.extras))
    async with get_sessionmaker()() as session:
        requests = (await session.execute(select(ApprovalRequest))).scalars().all()
    # The audit row of a call that runs stores the payload built here.
    assert _build_audit_payload(looked_up, "hash", "ok")["op_class"] == "credential_read"
    (request,) = requests
    assert request.proposed_effect["op_class"] == "credential_read"


@pytest.mark.asyncio
async def test_reingest_keeps_a_stored_class_when_the_floor_gives_none(
    stub_embedding_service: AsyncMock,
) -> None:
    register_ingest_safety_floor(product=_PRODUCT, impl_id=_IMPL_ID, floor=_credential_floor)
    await _register(stub_embedding_service)
    _FLOORS.clear()

    await _register(stub_embedding_service)

    assert (await _row(_CLIENTS)).op_class == "credential_read"


@pytest.mark.asyncio
async def test_reingest_without_the_floor_keeps_a_stricter_stored_policy(
    stub_embedding_service: AsyncMock,
) -> None:
    """A row with a stored class keeps its stricter risk level and approval flag."""
    register_ingest_safety_floor(product=_PRODUCT, impl_id=_IMPL_ID, floor=_credential_floor)
    await _register(stub_embedding_service)
    async with get_sessionmaker()() as session, session.begin():
        row = (
            await session.execute(
                select(EndpointDescriptor).where(
                    EndpointDescriptor.product == _PRODUCT, EndpointDescriptor.op_id == _CLIENTS
                )
            )
        ).scalar_one()
        row.safety_level = "dangerous"
        row.requires_approval = True
    _FLOORS.clear()

    await _register(stub_embedding_service)

    row = await _row(_CLIENTS)
    assert (row.op_class, row.safety_level, row.requires_approval) == (
        "credential_read",
        "dangerous",
        True,
    )
    health = await _row(_HEALTH)
    assert (health.op_class, health.safety_level, health.requires_approval) == (
        None,
        "safe",
        False,
    )


@pytest.mark.asyncio
async def test_reingest_stores_a_class_the_floor_gives_later(
    stub_embedding_service: AsyncMock,
) -> None:
    await _register(stub_embedding_service)
    assert (await _row(_CLIENTS)).op_class is None

    register_ingest_safety_floor(product=_PRODUCT, impl_id=_IMPL_ID, floor=_credential_floor)
    await _register(stub_embedding_service)

    row = await _row(_CLIENTS)
    assert row.op_class == "credential_read"
    assert (row.safety_level, row.requires_approval) == ("caution", True)


def test_stored_class_never_replaces_a_credential_or_audit_class() -> None:
    assert resolve_op_class("vault.kv.read", "write") == "credential_read"
    assert resolve_op_class(_CLIENTS, "credential_read") == "credential_read"
    assert resolve_op_class(_CLIENTS) == "read"
    assert (
        resolve_operation_op_class(
            _CLIENTS, source_kind="ingested", safety_level="caution", stored_op_class="write"
        )
        == "write"
    )
    assert (
        resolve_operation_op_class(
            "vault.kv.read", source_kind="ingested", safety_level="caution", stored_op_class="write"
        )
        == "credential_read"
    )


# -- compatibility ----------------------------------------------------------------


def _two_argument_floor(version: str, proto: EndpointDescriptorProto) -> EndpointDescriptorProto:
    if proto.method == "GET":
        return proto.model_copy(update={"safety_level": "caution", "requires_approval": True})
    return proto


@pytest.mark.asyncio
async def test_two_argument_floor_works_unchanged(stub_embedding_service: AsyncMock) -> None:
    register_ingest_safety_floor(product=_PRODUCT, impl_id=_IMPL_ID, floor=_two_argument_floor)

    await _register(stub_embedding_service)

    for op_id in (_CLIENTS, _HEALTH, _BROKEN):
        row = await _row(op_id)
        assert (row.safety_level, row.requires_approval, row.op_class) == ("caution", True, None)


@pytest.mark.asyncio
async def test_untouched_operations_keep_their_stored_policy(
    stub_embedding_service: AsyncMock,
) -> None:
    await _register(stub_embedding_service)

    for op_id in (_CLIENTS, _HEALTH, _BROKEN):
        row = await _row(op_id)
        assert (row.safety_level, row.requires_approval, row.op_class) == ("safe", False, None)
    assert "components" in (await _row(_CLIENTS)).response_schema  # type: ignore[operator]
    assert "components" not in (await _row(_HEALTH)).response_schema  # type: ignore[operator]


# -- refusing a class that is not allowed -------------------------------------------


@pytest.mark.parametrize("bad", ["credential_reads", "read", "audit_query", ""])
def test_ingest_refuses_a_class_that_is_not_allowed(bad: str) -> None:
    def floor(version: str, proto: EndpointDescriptorProto) -> EndpointDescriptorProto:
        return proto.model_copy(update={"op_class": bad})

    register_ingest_safety_floor(product=_PRODUCT, impl_id=_IMPL_ID, floor=floor)

    with pytest.raises(InvalidOpClassError, match=r"GET:/clients"):
        apply_safety_floor(
            product=_PRODUCT, version=_VERSION, impl_id=_IMPL_ID, proto=_ops()[_CLIENTS]
        )


@pytest.mark.asyncio
async def test_register_refuses_a_class_that_is_not_allowed_and_stores_nothing(
    stub_embedding_service: AsyncMock,
) -> None:
    def floor(version: str, proto: EndpointDescriptorProto) -> EndpointDescriptorProto:
        return proto.model_copy(update={"op_class": "nonsense"})

    register_ingest_safety_floor(product=_PRODUCT, impl_id=_IMPL_ID, floor=floor)

    with pytest.raises(InvalidOpClassError, match="nonsense"):
        await _register(stub_embedding_service)

    async with get_sessionmaker()() as session:
        rows = (
            (
                await session.execute(
                    select(EndpointDescriptor).where(EndpointDescriptor.product == _PRODUCT)
                )
            )
            .scalars()
            .all()
        )
    assert rows == []
