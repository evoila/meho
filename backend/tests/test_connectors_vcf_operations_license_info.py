# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for ``vrops.license.info``, the VCF Operations 9.x licence read.

The op reads the edition and the licence of a VCF Operations instance with
two calls (``GET /suite-api/api/product/licensing/edition`` and
``GET /suite-api/api/product/licensing/info``) and returns one object. These
tests check that:

* the op runs end to end through ``dispatch`` on a database with no ingested
  spec, against saved vendor answers (respx, no live system);
* the licence key never appears in clear text in the result;
* the op is ``safe`` with ``requires_approval=False``;
* only the 9.x connector registers it (the 8.x connector reuses
  ``VROPS_TYPED_OPS``, which does not hold it).

The saved answers are the examples of the pinned VCF Operations 9.0 Suite API
file, with a key in the classic five-groups shape.
"""

from __future__ import annotations

import json
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
from meho_backplane.connectors.vcf_operations.connector import _mask_license_key
from meho_backplane.connectors.vcf_operations.typed_ops import (
    VROPS_9_ONLY_TYPED_OPS,
    VROPS_LICENSE_INFO_OP,
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
_OP_ID = "vrops.license.info"

_VROPS_HOST = "vrops-license-info.test.invalid"
_VROPS_BASE_URL = f"https://{_VROPS_HOST}"

_ACQUIRE_PATH = "/suite-api/api/auth/token/acquire"
_EDITION_PATH = "/suite-api/api/product/licensing/edition"
_INFO_PATH = "/suite-api/api/product/licensing/info"
_OPS_TOKEN = "vrops-license-ops-token"

_LICENSE_KEY = "AAAAA-BBBBB-CCCCC-DDDDD-EEEEE"

_EDITION_RESPONSE: dict[str, Any] = {"productLicensingEdition": "ENTERPRISE"}
_INFO_RESPONSE: dict[str, Any] = {
    "licenseKey": _LICENSE_KEY,
    "licensed": True,
    "expirationDate": 1744473856630,
    "licenseName": "license-name",
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
        self.tenant_id: UUID = UUID("00000000-0000-0000-0000-00000000a0c3")
        self.name = "vrops-license-info"
        self.host = _VROPS_HOST
        self.port = 443
        self.secret_ref = "targets/op-license/vrops-license-info"
        self.auth_model = "shared_service_account"
        self.auth_source: str | None = None


def _make_operator() -> Operator:
    return Operator(
        sub="op-license-vrops",
        name="License Info vROps Operator",
        email=None,
        raw_jwt="op.license.vrops.jwt",
        tenant_id=UUID("00000000-0000-0000-0000-00000000a0c3"),
        tenant_role=TenantRole.OPERATOR,
    )


@pytest.mark.asyncio
async def test_license_info_dispatches_and_masks_the_key(
    _stub_embedding: AsyncMock, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One typed call returns edition and licence, with the key masked."""
    await register_vcf_operations_typed_operations(embedding_service=_stub_embedding)
    install_fake_client(monkeypatch, secret={"username": "svc-vrops", "password": "pw"})

    async with respx.mock(base_url=_VROPS_BASE_URL, assert_all_called=True) as mock:
        mock.post(_ACQUIRE_PATH).respond(200, json={"token": _OPS_TOKEN})
        edition_route = mock.get(_EDITION_PATH).respond(200, json=_EDITION_RESPONSE)
        info_route = mock.get(_INFO_PATH).respond(200, json=_INFO_RESPONSE)
        result = await dispatch(
            operator=_make_operator(),
            connector_id=_CONNECTOR_ID,
            op_id=_OP_ID,
            target=_Target(),
            params={},
        )

    assert result.status == "ok", result.error
    assert result.result == {
        "productLicensingEdition": "ENTERPRISE",
        "licensed": True,
        "expirationDate": 1744473856630,
        "licenseName": "license-name",
        "licenseKey": "************************EEEEE",
    }
    assert _LICENSE_KEY not in json.dumps(result.result)
    for route in (edition_route, info_route):
        assert route.call_count == 1
        assert route.calls[0].request.headers["authorization"] == f"OpsToken {_OPS_TOKEN}"


@pytest.mark.asyncio
async def test_license_info_without_a_key_passes_through(monkeypatch: pytest.MonkeyPatch) -> None:
    """An answer with no licenseKey field gets no licenseKey field added."""
    connector = VcfOperationsConnector()

    async def _fake_get_json(target: Any, path: str, **_: Any) -> dict[str, Any]:
        return _EDITION_RESPONSE if path == _EDITION_PATH else {"licensed": False}

    monkeypatch.setattr(connector, "_get_json", _fake_get_json)
    result = await connector.license_info(_make_operator(), _Target(), {})
    assert result == {"productLicensingEdition": "ENTERPRISE", "licensed": False}


@pytest.mark.parametrize(
    ("value", "masked"),
    [
        (_LICENSE_KEY, "************************EEEEE"),
        ("short-key", "*********"),
        ("0123456789", "**********"),
        ("", ""),
        (None, None),
    ],
)
def test_mask_license_key(value: Any, masked: Any) -> None:
    """A key shows at most its last 5 characters; a short key is hidden in full."""
    assert _mask_license_key(value) == masked


@pytest.mark.asyncio
async def test_license_info_is_safe_and_needs_no_approval(
    _stub_embedding: AsyncMock, session: AsyncSession
) -> None:
    """The op registers on the 9.x connector as a safe read with no approval step."""
    assert VROPS_LICENSE_INFO_OP.safety_level == "safe"
    assert VROPS_LICENSE_INFO_OP.requires_approval is False

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


def test_license_info_is_9x_only() -> None:
    """The 8.x connector reuses VROPS_TYPED_OPS, so the licence read is not in it."""
    assert VROPS_LICENSE_INFO_OP in VROPS_9_ONLY_TYPED_OPS
    assert VROPS_LICENSE_INFO_OP not in VROPS_TYPED_OPS
