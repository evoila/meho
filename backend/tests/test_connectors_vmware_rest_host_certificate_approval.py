# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The host certificate operations through the real dispatcher.

* every operation is registered on both vCenter catalogs (8.0 and 9.0) with
  its safety level and approval flag; the reads are ``safe``;
* a person's call of each change waits for a second person; nothing reaches
  vCenter before that;
* a private key in the install input is refused before the call waits, so no
  approval request stores it.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from typing import Any
from unittest.mock import AsyncMock
from uuid import UUID

import pytest
from sqlalchemy import func, select

from meho_backplane.auth.operator import Operator, PrincipalKind, TenantRole
from meho_backplane.connectors.registry import clear_registry, register_connector_v2
from meho_backplane.connectors.vmware_rest import VmwareRestConnector
from meho_backplane.connectors.vmware_rest.composites import (
    register_vmware_composite_operations,
    register_vmware_composite_operations_8_0,
)
from meho_backplane.connectors.vmware_rest.typed_ops import (
    register_vmware_typed_operations,
    register_vmware_typed_operations_8_0,
)
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import ApprovalRequest, EndpointDescriptor
from meho_backplane.db.models import Target as TargetORM
from meho_backplane.operations import reset_dispatcher_caches
from meho_backplane.operations._handler_resolve import _CONNECTOR_INSTANCE_CACHE
from meho_backplane.operations.meta_tools import call_operation
from meho_backplane.settings import get_settings
from tests._vmware_host_certificate_fake import (
    FAKE_KEY_BLOCK,
    CertVim,
    make_certificate,
    seed_host,
)

_CONNECTOR_ID = "vmware-rest-9.0"
_TENANT_ID = UUID("00000000-0000-0000-0000-000000004354")
_TARGET_NAME = "example-vcenter"
_HOST = "host-1001"
_PEM, _START, _END = make_certificate()

#: op id -> (safety level, requires approval, the params of one call)
_OPS: dict[str, tuple[str, bool, dict[str, Any]]] = {
    "vmware.host.certificate.show": ("safe", False, {"host": _HOST}),
    "vmware.host.tpm.attestation": ("safe", False, {"host": _HOST}),
    "vmware.composite.host.certificate.renew": ("dangerous", True, {"host": _HOST}),
    "vmware.composite.host.certificate.csr": ("caution", True, {"host": _HOST}),
    "vmware.composite.host.certificate.install": (
        "dangerous",
        True,
        {"host": _HOST, "certificate_pem": _PEM},
    ),
}
_CHANGES = sorted(op_id for op_id, (_level, approval, _params) in _OPS.items() if approval)
_VENDOR_WRITES = (
    "CertMgrRefreshCertificates_Task",
    "GenerateCertificateSigningRequest",
    "ReplaceCACertificatesAndCRLs",
    "InstallServerCertificate",
    "NotifyAffectedServices",
)


@pytest.fixture
def _dispatch_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
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


def _embedding_stub() -> AsyncMock:
    service = AsyncMock()
    service.encode_one.return_value = [0.1] * 384
    service.encode.return_value = [[0.1] * 384]
    service.dimension = 384
    return service


def _person(sub: str, kind: PrincipalKind = PrincipalKind.USER) -> Operator:
    return Operator(
        sub=sub,
        name="Host Certificate Test",
        email=None,
        raw_jwt="<test-raw-jwt>",
        tenant_id=_TENANT_ID,
        tenant_role=TenantRole.OPERATOR,
        principal_kind=kind,
    )


async def _bootstrap(conn: CertVim) -> None:
    register_connector_v2(
        product="vmware", version="9.0", impl_id="vmware-rest", cls=VmwareRestConnector
    )
    _CONNECTOR_INSTANCE_CACHE[VmwareRestConnector] = conn  # type: ignore[assignment]
    await register_vmware_composite_operations(embedding_service=_embedding_stub())
    async with get_sessionmaker()() as s:
        s.add(
            TargetORM(
                id=uuid.uuid4(),
                tenant_id=_TENANT_ID,
                name=_TARGET_NAME,
                aliases=[],
                product="vmware",
                host="vcenter.example.test",
                port=443,
                fqdn=None,
                secret_ref=None,
                auth_model="shared_service_account",
                vpn_required=False,
                extras={},
                fingerprint={"version": "9.0"},
                preferred_impl_id="vmware-rest",
                notes="seeded by test_connectors_vmware_rest_host_certificate_approval",
            )
        )
        await s.commit()


def _args(op_id: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "connector_id": _CONNECTOR_ID,
        "op_id": op_id,
        "target": _TARGET_NAME,
        "params": params if params is not None else _OPS[op_id][2],
    }


def _sent(conn: CertVim) -> list[str]:
    return [method for method in conn.methods() if method in _VENDOR_WRITES]


async def _approval_count() -> int:
    async with get_sessionmaker()() as s:
        return int(
            (await s.execute(select(func.count()).select_from(ApprovalRequest))).scalar_one()
        )


@pytest.mark.usefixtures("_dispatch_env")
async def test_each_operation_is_registered_with_its_level_on_both_catalogs() -> None:
    stub = _embedding_stub()
    await register_vmware_typed_operations(embedding_service=stub)
    await register_vmware_typed_operations_8_0(embedding_service=stub)
    await register_vmware_composite_operations(embedding_service=stub)
    await register_vmware_composite_operations_8_0(embedding_service=stub)
    async with get_sessionmaker()() as s:
        rows = (
            (
                await s.execute(
                    select(EndpointDescriptor).where(EndpointDescriptor.op_id.in_(list(_OPS)))
                )
            )
            .scalars()
            .all()
        )
    seen = {(row.op_id, row.version) for row in rows}
    assert seen == {(op_id, version) for op_id in _OPS for version in ("8.0", "9.0")}
    for row in rows:
        level, approval, _params = _OPS[row.op_id]
        assert row.safety_level == level, row.op_id
        assert row.requires_approval is approval, row.op_id


@pytest.mark.usefixtures("_dispatch_env")
@pytest.mark.parametrize("op_id", _CHANGES)
async def test_a_persons_call_waits_for_approval_and_sends_nothing(op_id: str) -> None:
    conn = CertVim()
    seed_host(conn)
    await _bootstrap(conn)
    call = await call_operation(_person("op-requester"), _args(op_id))
    assert call["status"] == "awaiting_approval", call
    assert _sent(conn) == []
    assert await _approval_count() == 1


@pytest.mark.usefixtures("_dispatch_env")
async def test_a_private_key_is_refused_before_the_call_waits() -> None:
    conn = CertVim()
    seed_host(conn)
    await _bootstrap(conn)
    key = FAKE_KEY_BLOCK
    call = await call_operation(
        _person("op-requester"),
        _args(
            "vmware.composite.host.certificate.install",
            {"host": _HOST, "certificate_pem": f"{_PEM}{key}"},
        ),
    )
    assert call["status"] != "awaiting_approval", call
    assert "invalid_params" in str(call)
    assert await _approval_count() == 0
    assert _sent(conn) == []
