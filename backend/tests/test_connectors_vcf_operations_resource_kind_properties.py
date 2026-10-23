# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for ``vrops.resource_kind.properties``, a VCF Operations 9.x read.

The op lists the properties one resource kind of one adapter kind has, with
``GET /suite-api/api/adapterkinds/{adapterKindKey}/resourcekinds/{resourceKindKey}/properties``.
These tests check that:

* the op runs end to end through ``dispatch`` on a database with no ingested
  spec, against a saved vendor answer (respx, no live system);
* ``keyContains`` keeps only the matching rows;
* the two keys are percent-encoded into the path;
* the op is ``safe`` with ``requires_approval=False``;
* only the 9.x connector registers it (the 8.x connector reuses
  ``VROPS_TYPED_OPS``, which does not hold it).

The saved answer follows the example of the pinned VCF Operations 9.0 Suite
API file (``resource-kind-attributes``).
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Iterator
from typing import Any
from unittest.mock import AsyncMock
from uuid import UUID

import pytest
import respx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors.registry import clear_registry, register_connector_v2
from meho_backplane.connectors.vcf_operations import (
    VcfOperationsConnector,
    register_vcf_operations_typed_operations,
)
from meho_backplane.connectors.vcf_operations.typed_ops import (
    VROPS_9_ONLY_TYPED_OPS,
    VROPS_RESOURCE_KIND_PROPERTIES_OP,
    VROPS_TYPED_OPS,
)
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import EndpointDescriptor
from meho_backplane.operations import dispatch, reset_dispatcher_caches
from meho_backplane.operations._handler_resolve import reset_handler_cache
from meho_backplane.settings import get_settings

from ._vault_fakes import install_fake_client

_PRODUCT = "vrops"
_VERSION = "9.0"
_IMPL_ID = "vrops-rest"
_CONNECTOR_ID = "vrops-rest-9.0"
_OP_ID = "vrops.resource_kind.properties"

_VROPS_HOST = "vrops-kind-properties.test.invalid"
_VROPS_BASE_URL = f"https://{_VROPS_HOST}"

_ACQUIRE_PATH = "/suite-api/api/auth/token/acquire"
_PROPERTIES_PATH = "/suite-api/api/adapterkinds/VMWARE/resourcekinds/VirtualMachine/properties"
_OPS_TOKEN = "vrops-kind-properties-ops-token"

_GUEST_ROW: dict[str, Any] = {
    "key": "config|guestFullName",
    "name": "Guest OS from VM configuration",
    "description": "Some description",
    "defaultMonitored": True,
    "rollupType": "LATEST",
    "instanceType": "INSTANCED",
    "dataType": "STRING",
    "dataType2": "STRING",
    "dtSubType": "common",
    "property": True,
}
_CPU_ROW: dict[str, Any] = {
    "key": "config|hardware|numCpu",
    "name": "Number of virtual CPUs",
    "description": "Some description",
    "defaultMonitored": True,
    "rollupType": "LATEST",
    "instanceType": "INSTANCED",
    "unit": "units",
    "dataType": "INTEGER",
    "dataType2": "INTEGER",
    "dtSubType": "common",
    "property": True,
}
_OS_NAME_ROW: dict[str, Any] = {
    "key": "summary|os",
    "name": "Guest operating system",
    "dataType": "STRING",
    "dataType2": "STRING",
    "property": True,
}
_PROPERTIES_RESPONSE: dict[str, Any] = {
    "resourceTypeAttributes": [_GUEST_ROW, _CPU_ROW, _OS_NAME_ROW],
}


@pytest.fixture(autouse=True)
def _settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Pin the env vars Settings reads (Vault client + dispatcher)."""
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    monkeypatch.setenv("VAULT_OIDC_ROLE", "meho-mcp")
    monkeypatch.setenv("VAULT_OIDC_MOUNT_PATH", "jwt")
    monkeypatch.setenv("VAULT_TIMEOUT_SECONDS", "5.0")
    monkeypatch.delenv("VAULT_NAMESPACE", raising=False)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _reset_module_state() -> Iterator[None]:
    """Reset dispatcher and handler caches and the connector registry."""
    reset_dispatcher_caches()
    reset_handler_cache()
    clear_registry()
    register_connector_v2(
        product=_PRODUCT, version=_VERSION, impl_id=_IMPL_ID, cls=VcfOperationsConnector
    )
    yield
    reset_dispatcher_caches()
    reset_handler_cache()
    clear_registry()


@pytest.fixture
def _stub_embedding(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    """Fixed embedding stub, so registration does not load the model."""
    service = AsyncMock()
    service.encode_one.return_value = [0.1] * 384
    service.encode.return_value = [[0.1] * 384]
    service.dimension = 384
    monkeypatch.setattr(
        "meho_backplane.operations.typed_register.encode_endpoint_text",
        AsyncMock(return_value=[0.1] * 384),
    )
    return service


@pytest.fixture
async def session() -> AsyncIterator[AsyncSession]:
    """A session on the per-worker test database."""
    async with get_sessionmaker()() as s:
        yield s


class _Target:
    """A target the connector and the resolver both accept."""

    def __init__(self) -> None:
        self.product = _PRODUCT
        self.fingerprint = type("_FP", (), {"version": _VERSION})()
        self.preferred_impl_id: str | None = None
        self.id: UUID = uuid.uuid4()
        self.tenant_id: UUID = UUID("00000000-0000-0000-0000-00000000a0c4")
        self.name = "vrops-kind-properties"
        self.host = _VROPS_HOST
        self.port = 443
        self.secret_ref = "targets/op-kind/vrops-kind-properties"
        self.auth_model = "shared_service_account"
        self.auth_source: str | None = None


def _make_operator() -> Operator:
    return Operator(
        sub="op-kind-vrops",
        name="Kind Properties vROps Operator",
        email=None,
        raw_jwt="op.kind.vrops.jwt",
        tenant_id=UUID("00000000-0000-0000-0000-00000000a0c4"),
        tenant_role=TenantRole.OPERATOR,
    )


async def _dispatch(params: dict[str, Any]) -> Any:
    return await dispatch(
        operator=_make_operator(),
        connector_id=_CONNECTOR_ID,
        op_id=_OP_ID,
        target=_Target(),
        params=params,
    )


@pytest.mark.asyncio
async def test_lists_the_properties_of_a_resource_kind(
    _stub_embedding: AsyncMock, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One typed call returns every property row of the resource kind."""
    await register_vcf_operations_typed_operations(embedding_service=_stub_embedding)
    install_fake_client(monkeypatch, secret={"username": "svc-vrops", "password": "pw"})

    async with respx.mock(base_url=_VROPS_BASE_URL, assert_all_called=True) as mock:
        mock.post(_ACQUIRE_PATH).respond(200, json={"token": _OPS_TOKEN})
        route = mock.get(_PROPERTIES_PATH).respond(200, json=_PROPERTIES_RESPONSE)
        result = await _dispatch({"adapterKindKey": "VMWARE", "resourceKindKey": "VirtualMachine"})

    assert result.status == "ok", result.error
    assert result.result == _PROPERTIES_RESPONSE
    assert route.call_count == 1
    request = route.calls[0].request
    assert request.headers["authorization"] == f"OpsToken {_OPS_TOKEN}"
    assert request.url.query == b""


@pytest.mark.asyncio
async def test_key_contains_keeps_only_matching_rows(
    _stub_embedding: AsyncMock, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """keyContains matches the key or the name, not case-sensitive."""
    await register_vcf_operations_typed_operations(embedding_service=_stub_embedding)
    install_fake_client(monkeypatch, secret={"username": "svc-vrops", "password": "pw"})

    async with respx.mock(base_url=_VROPS_BASE_URL, assert_all_called=True) as mock:
        mock.post(_ACQUIRE_PATH).respond(200, json={"token": _OPS_TOKEN})
        mock.get(_PROPERTIES_PATH).respond(200, json=_PROPERTIES_RESPONSE)
        result = await _dispatch(
            {
                "adapterKindKey": "VMWARE",
                "resourceKindKey": "VirtualMachine",
                "keyContains": "GUEST",
            }
        )

    assert result.status == "ok", result.error
    # _GUEST_ROW matches on its key, _OS_NAME_ROW only on its name.
    assert result.result == {"resourceTypeAttributes": [_GUEST_ROW, _OS_NAME_ROW]}


@pytest.mark.asyncio
async def test_keys_are_percent_encoded_into_the_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """A key with a space or a slash stays one path segment."""
    connector = VcfOperationsConnector()
    seen: list[str] = []

    async def _fake_get_json(target: Any, path: str, **_: Any) -> dict[str, Any]:
        seen.append(path)
        return {"resourceTypeAttributes": []}

    monkeypatch.setattr(connector, "_get_json", _fake_get_json)
    await connector.resource_kind_properties(
        _make_operator(),
        _Target(),
        {"adapterKindKey": "My Adapter", "resourceKindKey": "a/b"},
    )
    assert seen == [
        "/suite-api/api/adapterkinds/My%20Adapter/resourcekinds/a%2Fb/properties",
    ]


@pytest.mark.asyncio
async def test_missing_resource_kind_key_is_rejected_before_the_call(
    _stub_embedding: AsyncMock, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Both keys are required; a call without one never reaches the vendor."""
    await register_vcf_operations_typed_operations(embedding_service=_stub_embedding)
    install_fake_client(monkeypatch, secret={"username": "svc-vrops", "password": "pw"})

    async with respx.mock(base_url=_VROPS_BASE_URL, assert_all_called=False) as mock:
        route = mock.get(_PROPERTIES_PATH).respond(200, json=_PROPERTIES_RESPONSE)
        result = await _dispatch({"adapterKindKey": "VMWARE"})

    assert result.status != "ok"
    assert not route.called


@pytest.mark.asyncio
async def test_is_safe_and_needs_no_approval(
    _stub_embedding: AsyncMock, session: AsyncSession
) -> None:
    """The op registers on the 9.x connector as a safe read with no approval step."""
    assert VROPS_RESOURCE_KIND_PROPERTIES_OP.safety_level == "safe"
    assert VROPS_RESOURCE_KIND_PROPERTIES_OP.requires_approval is False

    await register_vcf_operations_typed_operations(embedding_service=_stub_embedding)
    row = (
        await session.execute(
            select(EndpointDescriptor).where(
                EndpointDescriptor.product == _PRODUCT,
                EndpointDescriptor.version == _VERSION,
                EndpointDescriptor.impl_id == _IMPL_ID,
                EndpointDescriptor.op_id == _OP_ID,
            )
        )
    ).scalar_one()
    assert row.source_kind == "typed"
    assert row.safety_level == "safe"
    assert row.requires_approval is False


def test_is_9x_only() -> None:
    """The 8.x connector reuses VROPS_TYPED_OPS, so this read is not in it."""
    assert VROPS_RESOURCE_KIND_PROPERTIES_OP in VROPS_9_ONLY_TYPED_OPS
    assert VROPS_RESOURCE_KIND_PROPERTIES_OP not in VROPS_TYPED_OPS
