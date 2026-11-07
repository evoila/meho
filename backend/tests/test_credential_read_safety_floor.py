# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The op class floor for ingested credential reads.

An ingested operation that a connector declares as ``credential_read`` (the
class that hides the values of its answer) is at least ``caution`` and waits
for a person's approval. The floor is part of ``apply_safety_floor``, so it
needs no catalog row and no ``safety_overlay`` list. These tests check that:

* a declared credential read comes out ``caution`` with approval, with no
  catalog row for its product;
* a read without the class keeps the parser's result;
* the floor only raises, and an overlay rule can raise it further;
* a credential read counts as governed, so registration stores the raised
  level and a re-ingest never lowers a stricter stored row.
"""

from __future__ import annotations

from collections.abc import Iterator
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select, update

from meho_backplane import op_classes
from meho_backplane.connectors.registry import clear_registry
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import EndpointDescriptor
from meho_backplane.operations.ingest import catalog as catalog_module
from meho_backplane.operations.ingest import parse_openapi, register_ingested_operations
from meho_backplane.operations.ingest.catalog import parse_catalog
from meho_backplane.operations.ingest.safety_floors import apply_safety_floor, has_safety_floor
from meho_backplane.operations.ingest.schemas import EndpointDescriptorProto
from meho_backplane.settings import get_settings

_PRODUCT = "secretfixture"
_VERSION = "1.0"
_IMPL_ID = "secretfixture-rest"

_SPEC = """
openapi: 3.1.0
info: {title: credential read fixture, version: '1.0'}
paths:
  /v1/accounts/{id}/password:
    get:
      operationId: accounts.password
      responses: {'200': {description: ok}}
  /v1/accounts:
    get:
      operationId: accounts.list
      responses: {'200': {description: ok}}
"""

_PASSWORD_READ = "GET:/v1/accounts/{id}/password"
_PLAIN_READ = "GET:/v1/accounts"

_OVERLAY_CATALOG = f"""
entries:
  - product: {_PRODUCT}
    version: "{_VERSION}"
    impl_id: {_IMPL_ID}
    requires_connector_class: SecretFixtureConnector
    safety_overlay:
      - method: GET
        path: /v1/accounts/{{id}}/password
        safety_level: dangerous
"""


@pytest.fixture(autouse=True)
def _required_settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _clear_connector_registry() -> Iterator[None]:
    clear_registry()
    yield
    clear_registry()


@pytest.fixture
def declared_password_read() -> Iterator[None]:
    """Declare the password read as a credential read for one test."""
    saved = dict(op_classes._DECLARED)
    saved_safe_writes = set(op_classes._SAFE_WRITES)
    op_classes.declare_op_classes(credential_read=(_PASSWORD_READ,))
    try:
        yield
    finally:
        op_classes._reset_declared_op_classes_for_testing()
        op_classes._DECLARED.update(saved)
        op_classes._SAFE_WRITES.update(saved_safe_writes)


@pytest.fixture
def overlay_catalog(monkeypatch: pytest.MonkeyPatch) -> None:
    """Serve a catalog whose only row raises the password read to dangerous."""
    catalog = parse_catalog(_OVERLAY_CATALOG)
    monkeypatch.setattr(catalog_module, "load_catalog", lambda: catalog)


@pytest.fixture
def stub_embedding_service() -> AsyncMock:
    service = AsyncMock()
    service.encode_one.return_value = [0.25] * 384
    service.encode.return_value = [[0.25] * 384]
    service.dimension = 384
    return service


def _parsed_ops() -> dict[str, EndpointDescriptorProto]:
    rows = parse_openapi("file:///credential-read-fixture.yaml", content=_SPEC)
    return {row.op_id: row for row in rows}


def _apply(proto: EndpointDescriptorProto) -> EndpointDescriptorProto:
    return apply_safety_floor(product=_PRODUCT, version=_VERSION, impl_id=_IMPL_ID, proto=proto)


def _governed(proto: EndpointDescriptorProto) -> bool:
    return has_safety_floor(product=_PRODUCT, version=_VERSION, impl_id=_IMPL_ID, proto=proto)


# -- applying the floor -------------------------------------------------------


def test_product_has_no_catalog_row() -> None:
    """The fixture product is in no catalog, so no overlay list can play a part."""
    assert catalog_module.load_catalog().get(_PRODUCT, _VERSION) is None


@pytest.mark.usefixtures("declared_password_read")
def test_declared_credential_read_is_caution_with_approval() -> None:
    parsed = _parsed_ops()[_PASSWORD_READ]
    assert (parsed.safety_level, parsed.requires_approval) == ("safe", False)

    result = _apply(parsed)

    assert (result.safety_level, result.requires_approval) == ("caution", True)


@pytest.mark.usefixtures("declared_password_read")
def test_declared_credential_read_is_governed() -> None:
    assert _governed(_parsed_ops()[_PASSWORD_READ])


@pytest.mark.usefixtures("declared_password_read")
def test_read_without_the_class_keeps_the_parser_result() -> None:
    plain = _parsed_ops()[_PLAIN_READ]

    assert _apply(plain) is plain
    assert not _governed(plain)


def test_undeclared_password_read_keeps_the_parser_result() -> None:
    """Without the declaration the floor does not guess from the path."""
    parsed = _parsed_ops()[_PASSWORD_READ]

    assert _apply(parsed) is parsed


@pytest.mark.usefixtures("declared_password_read")
@pytest.mark.parametrize(
    ("level", "approval"),
    [("dangerous", False), ("dangerous", True), ("destructive", True)],
)
def test_floor_keeps_a_stricter_level_and_turns_approval_on(level: str, approval: bool) -> None:
    proto = EndpointDescriptorProto(
        op_id=_PASSWORD_READ,
        method="GET",
        path="/v1/accounts/{id}/password",
        safety_level=level,  # type: ignore[arg-type]
        requires_approval=approval,
    )

    result = _apply(proto)

    assert (result.safety_level, result.requires_approval) == (level, True)


@pytest.mark.usefixtures("declared_password_read", "overlay_catalog")
def test_overlay_raises_a_credential_read_further() -> None:
    result = _apply(_parsed_ops()[_PASSWORD_READ])

    assert (result.safety_level, result.requires_approval) == ("dangerous", True)


# -- registration and re-ingest ----------------------------------------------


async def _stored(op_id: str) -> tuple[str, bool]:
    async with get_sessionmaker()() as session:
        row = (
            await session.execute(
                select(EndpointDescriptor).where(
                    EndpointDescriptor.product == _PRODUCT,
                    EndpointDescriptor.op_id == op_id,
                )
            )
        ).scalar_one()
        return row.safety_level, row.requires_approval


async def _register(service: AsyncMock) -> None:
    await register_ingested_operations(
        product=_PRODUCT,
        version=_VERSION,
        impl_id=_IMPL_ID,
        spec_source="credential-read-fixture.yaml",
        operations=list(_parsed_ops().values()),
        embedding_service=service,
    )


@pytest.mark.asyncio
@pytest.mark.usefixtures("declared_password_read")
async def test_register_stores_caution_with_approval(stub_embedding_service: AsyncMock) -> None:
    await _register(stub_embedding_service)

    assert await _stored(_PASSWORD_READ) == ("caution", True)
    assert await _stored(_PLAIN_READ) == ("safe", False)


@pytest.mark.asyncio
@pytest.mark.usefixtures("declared_password_read")
async def test_reingest_keeps_a_stricter_stored_row(stub_embedding_service: AsyncMock) -> None:
    await _register(stub_embedding_service)
    async with get_sessionmaker()() as session:
        await session.execute(
            update(EndpointDescriptor)
            .where(
                EndpointDescriptor.product == _PRODUCT,
                EndpointDescriptor.op_id == _PASSWORD_READ,
            )
            .values(safety_level="dangerous")
        )
        await session.commit()

    await _register(stub_embedding_service)

    assert await _stored(_PASSWORD_READ) == ("dangerous", True)


@pytest.mark.asyncio
async def test_reingest_raises_a_row_stored_before_the_declaration(
    stub_embedding_service: AsyncMock,
) -> None:
    """A row stored while the read had no class is raised by the next ingest."""
    await _register(stub_embedding_service)
    assert await _stored(_PASSWORD_READ) == ("safe", False)

    saved = dict(op_classes._DECLARED)
    op_classes.declare_op_classes(credential_read=(_PASSWORD_READ,))
    try:
        await _register(stub_embedding_service)
    finally:
        op_classes._DECLARED.clear()
        op_classes._DECLARED.update(saved)

    assert await _stored(_PASSWORD_READ) == ("caution", True)
