# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The floor for operations that connect to an address the caller gives.

A parameter marked ``x-meho-caller-address: true`` makes its operation at
least ``caution`` with approval. These tests use fixture operations, so they
hold without any connector package:

* the marker counts at any depth of the parameter schema;
* registering a typed operation stores the raised values, keeps a stricter
  level, and a new start never lowers it;
* an ingested operation that a spec correction marks is raised and counts as
  governed, so a new ingest never lowers it;
* at dispatch, an override that turns approval off does not remove it, and a
  row stored with lower values is raised.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from typing import Any
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select, update

from meho_backplane.connectors.registry import clear_registry
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import EndpointDescriptor, OperationPolicyOverride
from meho_backplane.operations._lookup import lookup_descriptor
from meho_backplane.operations.caller_address import (
    CALLER_ADDRESS_MARKER,
    has_caller_address,
    raise_for_caller_address,
)
from meho_backplane.operations.ingest import (
    parse_openapi,
    register_ingested_operations,
    spec_corrections,
)
from meho_backplane.operations.ingest.safety_floors import apply_safety_floor, has_safety_floor
from meho_backplane.operations.ingest.schemas import EndpointDescriptorProto
from meho_backplane.operations.typed_register import register_typed_operation
from meho_backplane.settings import get_settings

_TYPED = {"product": "addrfixture", "version": "1.x", "impl_id": "addrfixture"}
_TYPED_OP = "addrfixture.probe.check"

_INGEST = {"product": "addringest", "version": "1.0", "impl_id": "addringest-rest"}
_SPEC = """
openapi: 3.1.0
info: {title: caller address fixture, version: '1.0'}
paths:
  /v1/fetch:
    get:
      operationId: fetch
      parameters:
        - {name: url, in: query, required: true, schema: {type: string}}
      responses: {'200': {description: ok}}
  /v1/items:
    get:
      operationId: items.list
      responses: {'200': {description: ok}}
"""
_FETCH = "GET:/v1/fetch"
_ITEMS = "GET:/v1/items"

_MARKED_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"host": {"type": "string", CALLER_ADDRESS_MARKER: True}},
}


@pytest.fixture(autouse=True)
def _settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _clean_registry() -> Iterator[None]:
    clear_registry()
    yield
    clear_registry()


@pytest.fixture
def embedding() -> AsyncMock:
    service = AsyncMock()
    service.encode_one.return_value = [0.25] * 384
    service.encode.return_value = [[0.25] * 384]
    service.dimension = 384
    return service


def _mark_url(version: str, proto: EndpointDescriptorProto) -> EndpointDescriptorProto:
    """A spec correction that marks the ``url`` query parameter of the fetch op."""
    if proto.op_id != _FETCH:
        return proto
    schema = dict(proto.parameter_schema)
    properties = dict(schema["properties"])  # type: ignore[call-overload]
    properties["url"] = {**properties["url"], CALLER_ADDRESS_MARKER: True}
    return proto.model_copy(update={"parameter_schema": {**schema, "properties": properties}})


@pytest.fixture
def marking_correction() -> Iterator[None]:
    key = (_INGEST["product"], _INGEST["impl_id"])
    spec_corrections.register_ingest_spec_correction(
        product=key[0], impl_id=key[1], correction=_mark_url
    )
    try:
        yield
    finally:
        spec_corrections._CORRECTIONS.pop(key, None)


# -- the marker ---------------------------------------------------------------


@pytest.mark.parametrize(
    "schema",
    [
        _MARKED_SCHEMA,
        {"properties": {"probe": {"properties": {"url": {CALLER_ADDRESS_MARKER: True}}}}},
        {"properties": {"hosts": {"type": "array", "items": {CALLER_ADDRESS_MARKER: True}}}},
        {"properties": {"to": {"anyOf": [{"type": "null"}, {CALLER_ADDRESS_MARKER: True}]}}},
    ],
    ids=["top-level", "nested-object", "array-item", "any-of"],
)
def test_marker_counts_at_any_depth(schema: dict[str, Any]) -> None:
    assert has_caller_address(schema)


@pytest.mark.parametrize(
    "schema",
    [
        {},
        {"properties": {"host": {"type": "string"}}},
        {"properties": {"host": {CALLER_ADDRESS_MARKER: False}}},
        {"properties": {"host": {CALLER_ADDRESS_MARKER: "true"}}},
    ],
    ids=["empty", "no-marker", "marker-false", "marker-not-a-bool"],
)
def test_no_marker_keeps_the_values(schema: dict[str, Any]) -> None:
    assert not has_caller_address(schema)
    assert raise_for_caller_address(schema, safety_level="safe", requires_approval=False) == (
        "safe",
        False,
    )


@pytest.mark.parametrize(
    ("level", "approval", "expected"),
    [
        ("safe", False, ("caution", True)),
        ("caution", False, ("caution", True)),
        ("dangerous", False, ("dangerous", True)),
        ("destructive", True, ("destructive", True)),
    ],
)
def test_floor_only_raises(level: str, approval: bool, expected: tuple[str, bool]) -> None:
    assert (
        raise_for_caller_address(_MARKED_SCHEMA, safety_level=level, requires_approval=approval)
        == expected
    )


# -- typed registration -------------------------------------------------------


async def _fixture_handler(target: Any, params: dict[str, Any]) -> dict[str, Any]:
    return {}


async def _register_typed(
    embedding: AsyncMock, *, safety_level: Any = "safe", schema: dict[str, Any] | None = None
) -> None:
    await register_typed_operation(
        **_TYPED,
        op_id=_TYPED_OP,
        handler=_fixture_handler,
        summary="Check a host.",
        description="Check a host.",
        parameter_schema=_MARKED_SCHEMA if schema is None else schema,
        when_to_use=None,
        safety_level=safety_level,
        requires_approval=False,
        embedding_service=embedding,
    )


async def _stored(product: str, op_id: str) -> tuple[str, bool]:
    async with get_sessionmaker()() as session:
        row = (
            await session.execute(
                select(EndpointDescriptor).where(
                    EndpointDescriptor.tenant_id.is_(None),
                    EndpointDescriptor.product == product,
                    EndpointDescriptor.op_id == op_id,
                )
            )
        ).scalar_one()
        return row.safety_level, row.requires_approval


@pytest.mark.asyncio
async def test_typed_registration_stores_caution_with_approval(embedding: AsyncMock) -> None:
    await _register_typed(embedding)

    assert await _stored(_TYPED["product"], _TYPED_OP) == ("caution", True)


@pytest.mark.asyncio
async def test_typed_registration_keeps_a_stricter_level(embedding: AsyncMock) -> None:
    await _register_typed(embedding, safety_level="dangerous")

    assert await _stored(_TYPED["product"], _TYPED_OP) == ("dangerous", True)


@pytest.mark.asyncio
async def test_typed_registration_without_marker_stays_safe(embedding: AsyncMock) -> None:
    await _register_typed(embedding, schema={"properties": {"host": {"type": "string"}}})

    assert await _stored(_TYPED["product"], _TYPED_OP) == ("safe", False)


@pytest.mark.asyncio
async def test_new_start_never_lowers_a_marked_typed_row(embedding: AsyncMock) -> None:
    await _register_typed(embedding)
    await _register_typed(embedding)

    assert await _stored(_TYPED["product"], _TYPED_OP) == ("caution", True)


# -- ingest -------------------------------------------------------------------


def _parsed() -> list[EndpointDescriptorProto]:
    return list(parse_openapi("file:///caller-address-fixture.yaml", content=_SPEC))


async def _ingest(embedding: AsyncMock) -> None:
    await register_ingested_operations(
        **_INGEST,
        spec_source="caller-address-fixture.yaml",
        operations=_parsed(),
        embedding_service=embedding,
    )


def test_unmarked_ingested_op_is_not_governed() -> None:
    fetch = next(p for p in _parsed() if p.op_id == _FETCH)

    assert apply_safety_floor(**_INGEST, proto=fetch) is fetch
    assert not has_safety_floor(**_INGEST, proto=fetch)


def test_marked_ingested_op_is_raised_and_governed() -> None:
    fetch = _mark_url(_INGEST["version"], next(p for p in _parsed() if p.op_id == _FETCH))

    floored = apply_safety_floor(**_INGEST, proto=fetch)

    assert (floored.safety_level, floored.requires_approval) == ("caution", True)
    assert has_safety_floor(**_INGEST, proto=fetch)


@pytest.mark.asyncio
@pytest.mark.usefixtures("marking_correction")
async def test_ingest_stores_caution_with_approval(embedding: AsyncMock) -> None:
    await _ingest(embedding)

    assert await _stored(_INGEST["product"], _FETCH) == ("caution", True)
    assert await _stored(_INGEST["product"], _ITEMS) == ("safe", False)


@pytest.mark.asyncio
@pytest.mark.usefixtures("marking_correction")
async def test_new_ingest_never_lowers_a_marked_row(embedding: AsyncMock) -> None:
    await _ingest(embedding)
    async with get_sessionmaker()() as session:
        await session.execute(
            update(EndpointDescriptor)
            .where(
                EndpointDescriptor.product == _INGEST["product"],
                EndpointDescriptor.op_id == _FETCH,
            )
            .values(safety_level="dangerous")
        )
        await session.commit()

    await _ingest(embedding)

    assert await _stored(_INGEST["product"], _FETCH) == ("dangerous", True)


# -- dispatch -----------------------------------------------------------------


async def _effective(tenant_id: uuid.UUID) -> tuple[str, bool]:
    row = await lookup_descriptor(tenant_id=tenant_id, op_id=_TYPED_OP, **_TYPED)
    assert row is not None
    return row.safety_level, row.requires_approval


@pytest.mark.asyncio
async def test_override_that_turns_approval_off_does_not_remove_it(
    embedding: AsyncMock,
) -> None:
    await _register_typed(embedding)
    async with get_sessionmaker()() as session:
        session.add(
            OperationPolicyOverride(
                tenant_id=None,
                **_TYPED,
                op_id=_TYPED_OP,
                requires_approval=False,
                reason="fixture: approval turned off",
                set_by_sub="fixture-admin",
            )
        )
        await session.commit()

    assert await _effective(uuid.uuid4()) == ("caution", True)


@pytest.mark.asyncio
async def test_row_stored_with_lower_values_is_raised_at_dispatch(embedding: AsyncMock) -> None:
    await _register_typed(embedding)
    async with get_sessionmaker()() as session:
        await session.execute(
            update(EndpointDescriptor)
            .where(EndpointDescriptor.op_id == _TYPED_OP)
            .values(safety_level="safe", requires_approval=False)
        )
        await session.commit()

    assert await _effective(uuid.uuid4()) == ("caution", True)
    assert await _stored(_TYPED["product"], _TYPED_OP) == ("safe", False)
