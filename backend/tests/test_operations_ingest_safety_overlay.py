# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Per-spec safety overlay declared in ``catalog.yaml`` (#3340).

The overlay lets a catalog row raise the safety level and turn on approval
for chosen ingested ops. These tests check that:

* the catalog schema accepts a valid overlay and rejects bad ones;
* the overlay raises matching ops and leaves other ops unchanged;
* the overlay only raises (it never lowers a level or clears approval);
* registration and re-ingest apply it to stored rows the same way.
"""

from __future__ import annotations

from collections.abc import Iterator
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select, update

from meho_backplane.connectors.registry import clear_registry
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import EndpointDescriptor
from meho_backplane.operations.ingest import catalog as catalog_module
from meho_backplane.operations.ingest import parse_openapi, register_ingested_operations
from meho_backplane.operations.ingest.catalog import CatalogError, parse_catalog
from meho_backplane.operations.ingest.safety_floors import apply_safety_floor, has_safety_floor
from meho_backplane.operations.ingest.schemas import EndpointDescriptorProto
from meho_backplane.settings import get_settings

_PRODUCT = "vimfixture"
_VERSION = "1.0"
_IMPL_ID = "vimfixture-rest"

_CATALOG_HEAD = f"""
entries:
  - product: {_PRODUCT}
    version: "{_VERSION}"
    impl_id: {_IMPL_ID}
    requires_connector_class: VimFixtureConnector
"""

_OVERLAY = """
    safety_overlay:
      - method: post
        path: /VirtualMachine/{moId}/PowerOnVM_Task
        safety_level: dangerous
        requires_approval: true
      - method: POST
        path: /VirtualMachine/{moId}/*Snapshot*
        safety_level: caution
        requires_approval: true
      - method: POST
        path: /VirtualMachine/{moId}/Destroy_Task
        safety_level: caution
"""

_VI_JSON_FIXTURE = """
openapi: 3.1.0
info: {title: VIM overlay fixture, version: '1.0'}
paths:
  /VirtualMachine/{moId}/PowerOnVM_Task:
    post:
      operationId: VirtualMachine.PowerOnVM_Task
      responses: {'200': {description: accepted}}
  /VirtualMachine/{moId}/CreateSnapshot_Task:
    post:
      operationId: VirtualMachine.CreateSnapshot_Task
      responses: {'200': {description: accepted}}
  /VirtualMachine/{moId}/Destroy_Task:
    post:
      operationId: VirtualMachine.Destroy_Task
      responses: {'200': {description: accepted}}
  /VirtualMachine/{moId}/Reset_Task:
    post:
      operationId: VirtualMachine.Reset_Task
      responses: {'200': {description: accepted}}
  /VirtualMachine/{moId}/runtime:
    get:
      operationId: VirtualMachine.runtime
      responses: {'200': {description: ok}}
"""

_POWER_ON = "POST:/VirtualMachine/{moId}/PowerOnVM_Task"
_SNAPSHOT = "POST:/VirtualMachine/{moId}/CreateSnapshot_Task"
_DESTROY = "POST:/VirtualMachine/{moId}/Destroy_Task"
_RESET = "POST:/VirtualMachine/{moId}/Reset_Task"
_RUNTIME = "GET:/VirtualMachine/{moId}/runtime"


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
def overlay_catalog(monkeypatch: pytest.MonkeyPatch) -> None:
    """Serve a catalog whose only row declares the test overlay."""
    catalog = parse_catalog(_CATALOG_HEAD + _OVERLAY)
    monkeypatch.setattr(catalog_module, "load_catalog", lambda: catalog)


@pytest.fixture
def plain_catalog(monkeypatch: pytest.MonkeyPatch) -> None:
    """Serve a catalog whose only row declares no overlay."""
    catalog = parse_catalog(_CATALOG_HEAD)
    monkeypatch.setattr(catalog_module, "load_catalog", lambda: catalog)


@pytest.fixture
def stub_embedding_service() -> AsyncMock:
    service = AsyncMock()
    service.encode_one.return_value = [0.25] * 384
    service.encode.return_value = [[0.25] * 384]
    service.dimension = 384
    return service


def _parsed_ops() -> dict[str, EndpointDescriptorProto]:
    rows = parse_openapi("file:///vi-json-overlay.yaml", content=_VI_JSON_FIXTURE)
    return {row.op_id: row for row in rows}


def _apply(proto: EndpointDescriptorProto) -> EndpointDescriptorProto:
    return apply_safety_floor(product=_PRODUCT, version=_VERSION, impl_id=_IMPL_ID, proto=proto)


# -- catalog schema -----------------------------------------------------------


def test_catalog_parses_overlay_and_normalises_method() -> None:
    entry = parse_catalog(_CATALOG_HEAD + _OVERLAY).get(_PRODUCT, _VERSION)
    assert entry is not None and entry.safety_overlay is not None
    first = entry.safety_overlay[0]
    assert first.method == "POST"
    assert first.safety_level == "dangerous"
    assert first.requires_approval is True
    assert entry.safety_overlay[2].requires_approval is False


@pytest.mark.parametrize(
    ("overlay", "message"),
    [
        ("    safety_overlay: []\n", "non-empty list"),
        (
            "    safety_overlay:\n"
            "      - {method: POST, path: /a, safety_level: caution}\n"
            "      - {method: post, path: /a, safety_level: dangerous}\n",
            r"unique on \(method, path\)",
        ),
        (
            "    safety_overlay:\n      - {method: POST, path: /a, safety_level: risky}\n",
            "safety_level",
        ),
        (
            "    safety_overlay:\n      - {method: POST, path: a/b, safety_level: caution}\n",
            "must start with '/'",
        ),
        (
            "    safety_overlay:\n"
            "      - {method: POST, path: /a, safety_level: caution, note: x}\n",
            "note",
        ),
    ],
)
def test_catalog_rejects_bad_overlay(overlay: str, message: str) -> None:
    with pytest.raises(CatalogError, match=message):
        parse_catalog(_CATALOG_HEAD + overlay)


def test_shipped_catalog_overlays_parse() -> None:
    """Every overlay in the shipped catalog loads (or there is none)."""
    catalog = catalog_module.load_catalog()
    for entry in catalog.entries:
        for rule in entry.safety_overlay or ():
            assert rule.path.startswith("/")


# -- applying the overlay -----------------------------------------------------


@pytest.mark.usefixtures("overlay_catalog")
def test_overlay_raises_matching_vi_json_ops() -> None:
    ops = _parsed_ops()
    assert ops[_POWER_ON].safety_level == "caution"
    assert ops[_POWER_ON].requires_approval is False

    power_on = _apply(ops[_POWER_ON])
    assert (power_on.safety_level, power_on.requires_approval) == ("dangerous", True)

    snapshot = _apply(ops[_SNAPSHOT])
    assert (snapshot.safety_level, snapshot.requires_approval) == ("caution", True)


@pytest.mark.usefixtures("overlay_catalog")
def test_overlay_leaves_unmatched_ops_unchanged() -> None:
    ops = _parsed_ops()
    for op_id in (_RESET, _RUNTIME):
        assert _apply(ops[op_id]) is ops[op_id]
        assert not has_safety_floor(
            product=_PRODUCT, version=_VERSION, impl_id=_IMPL_ID, proto=ops[op_id]
        )


@pytest.mark.usefixtures("overlay_catalog")
def test_overlay_never_lowers_level_or_clears_approval() -> None:
    """The Destroy_Task rule asks for caution with no approval; the parser's
    stricter dangerous + approval result must win."""
    destroy = _parsed_ops()[_DESTROY]
    assert (destroy.safety_level, destroy.requires_approval) == ("dangerous", True)

    result = _apply(destroy)
    assert (result.safety_level, result.requires_approval) == ("dangerous", True)


@pytest.mark.usefixtures("overlay_catalog")
def test_overlay_marks_matched_ops_as_governed() -> None:
    """A matched op is governed, so re-ingest uses the raise-only update."""
    power_on = _parsed_ops()[_POWER_ON]
    assert has_safety_floor(product=_PRODUCT, version=_VERSION, impl_id=_IMPL_ID, proto=power_on)


@pytest.mark.usefixtures("overlay_catalog")
def test_overlay_is_scoped_to_its_catalog_row() -> None:
    power_on = _parsed_ops()[_POWER_ON]
    for product, version in ((_PRODUCT, "2.0"), ("other", _VERSION)):
        result = apply_safety_floor(
            product=product, version=version, impl_id=_IMPL_ID, proto=power_on
        )
        assert result is power_on


@pytest.mark.usefixtures("overlay_catalog")
def test_overlay_matches_query_string_and_lowercase_method() -> None:
    proto = EndpointDescriptorProto(
        op_id="post:/VirtualMachine/{moId}/PowerOnVM_Task?host=x",
        method="post",
        path="/VirtualMachine/{moId}/PowerOnVM_Task?host=x",
        safety_level="caution",
    )
    result = _apply(proto)
    assert (result.safety_level, result.requires_approval) == ("dangerous", True)


@pytest.mark.usefixtures("plain_catalog")
def test_no_overlay_keeps_parser_result() -> None:
    power_on = _parsed_ops()[_POWER_ON]
    assert _apply(power_on) is power_on


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
        spec_source="vi-json-overlay.yaml",
        operations=list(_parsed_ops().values()),
        embedding_service=service,
    )


@pytest.mark.asyncio
@pytest.mark.usefixtures("overlay_catalog")
async def test_register_stores_overlay_result(stub_embedding_service: AsyncMock) -> None:
    await _register(stub_embedding_service)

    assert await _stored(_POWER_ON) == ("dangerous", True)
    assert await _stored(_SNAPSHOT) == ("caution", True)
    assert await _stored(_DESTROY) == ("dangerous", True)
    assert await _stored(_RESET) == ("caution", False)


@pytest.mark.asyncio
async def test_reingest_applies_a_new_overlay_to_existing_rows(
    monkeypatch: pytest.MonkeyPatch, stub_embedding_service: AsyncMock
) -> None:
    """Rows stored before the overlay existed are raised by the next ingest."""
    monkeypatch.setattr(catalog_module, "load_catalog", lambda: parse_catalog(_CATALOG_HEAD))
    await _register(stub_embedding_service)
    assert await _stored(_POWER_ON) == ("caution", False)

    overlay = parse_catalog(_CATALOG_HEAD + _OVERLAY)
    monkeypatch.setattr(catalog_module, "load_catalog", lambda: overlay)
    await _register(stub_embedding_service)
    assert await _stored(_POWER_ON) == ("dangerous", True)


@pytest.mark.asyncio
@pytest.mark.usefixtures("overlay_catalog")
async def test_reingest_keeps_a_stricter_stored_row(stub_embedding_service: AsyncMock) -> None:
    """An overlay-governed row that is already stricter is not lowered."""
    await _register(stub_embedding_service)
    async with get_sessionmaker()() as session:
        await session.execute(
            update(EndpointDescriptor)
            .where(EndpointDescriptor.product == _PRODUCT, EndpointDescriptor.op_id == _POWER_ON)
            .values(safety_level="destructive")
        )
        await session.commit()

    await _register(stub_embedding_service)
    assert await _stored(_POWER_ON) == ("destructive", True)
