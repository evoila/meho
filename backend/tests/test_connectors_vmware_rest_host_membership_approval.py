# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The host membership changes through the real dispatcher: each one waits for approval.

* every operation is registered on both vCenter catalogs (8.0 and 9.0) with
  its safety level and ``requires_approval: true``;
* a person's call of each change waits for a second person; nothing reaches
  vCenter before that;
* ``host.remove`` is destructive: without a matching ``preview_hash`` it is
  refused, a host that does not exist is refused before it waits, the
  approval card shows the blast radius, and an agent is always refused.
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
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import ApprovalRequest, EndpointDescriptor
from meho_backplane.db.models import Target as TargetORM
from meho_backplane.operations import dispatch, reset_dispatcher_caches
from meho_backplane.operations._handler_resolve import _CONNECTOR_INSTANCE_CACHE
from meho_backplane.operations.meta_tools import call_operation, preview_operation
from meho_backplane.settings import get_settings
from tests._vmware_host_membership_fake import CLUSTER, HOST, OTHER_CLUSTER, HostVim, seed_host

_CONNECTOR_ID = "vmware-rest-9.0"
_TENANT_ID = UUID("00000000-0000-0000-0000-000000004349")
_TARGET_NAME = "example-vcenter"

#: op id -> (safety level, the params of one call)
_CHANGES: dict[str, tuple[str, dict[str, Any]]] = {
    "vmware.composite.host.add": (
        "dangerous",
        {
            "cluster": CLUSTER,
            "host_name": "<esxi-host-name>",
            "password_secret_ref": "<vault-path-of-the-host-password>",
            "ssl_thumbprint": ":".join(["AB"] * 20),
        },
    ),
    "vmware.composite.host.move": ("dangerous", {"host": HOST, "cluster": OTHER_CLUSTER}),
    "vmware.composite.host.connect": ("caution", {"host": HOST}),
    "vmware.composite.host.disconnect": ("dangerous", {"host": HOST}),
    "vmware.composite.host.remove": ("destructive", {"host": HOST}),
}
_REMOVE = "vmware.composite.host.remove"
_VENDOR_WRITES = (
    "AddHost_Task",
    "MoveHostInto_Task",
    "ReconnectHost_Task",
    "DisconnectHost_Task",
    "Destroy_Task",
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
        name="Host Membership Test",
        email=None,
        raw_jwt="<test-raw-jwt>",
        tenant_id=_TENANT_ID,
        tenant_role=TenantRole.OPERATOR,
        principal_kind=kind,
    )


async def _bootstrap(conn: HostVim) -> None:
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
                notes="seeded by test_connectors_vmware_rest_host_membership_approval",
            )
        )
        await s.commit()


def _args(op_id: str) -> dict[str, Any]:
    return {
        "connector_id": _CONNECTOR_ID,
        "op_id": op_id,
        "target": _TARGET_NAME,
        "params": _CHANGES[op_id][1],
    }


def _sent(conn: HostVim) -> list[str]:
    return [method for method in conn.methods() if method in _VENDOR_WRITES]


async def _approval_count() -> int:
    async with get_sessionmaker()() as s:
        return int(
            (await s.execute(select(func.count()).select_from(ApprovalRequest))).scalar_one()
        )


@pytest.mark.usefixtures("_dispatch_env")
async def test_each_change_is_registered_with_its_level_on_both_catalogs() -> None:
    await register_vmware_composite_operations(embedding_service=_embedding_stub())
    await register_vmware_composite_operations_8_0(embedding_service=_embedding_stub())
    async with get_sessionmaker()() as s:
        rows = (
            (
                await s.execute(
                    select(EndpointDescriptor).where(EndpointDescriptor.op_id.in_(list(_CHANGES)))
                )
            )
            .scalars()
            .all()
        )
    seen = {(row.op_id, row.version) for row in rows}
    assert seen == {(op_id, version) for op_id in _CHANGES for version in ("8.0", "9.0")}
    for row in rows:
        assert row.safety_level == _CHANGES[row.op_id][0], row.op_id
        assert row.requires_approval is True, row.op_id
        assert row.source_kind == "composite"


@pytest.mark.usefixtures("_dispatch_env")
@pytest.mark.parametrize("op_id", sorted(set(_CHANGES) - {_REMOVE}))
async def test_a_persons_call_waits_for_approval_and_sends_nothing(op_id: str) -> None:
    conn = HostVim()
    seed_host(conn)
    await _bootstrap(conn)
    call = await call_operation(_person("op-requester"), _args(op_id))
    assert call["status"] == "awaiting_approval", call
    assert _sent(conn) == []
    assert await _approval_count() == 1


@pytest.mark.usefixtures("_dispatch_env")
async def test_remove_waits_for_approval_with_the_blast_radius() -> None:
    conn = HostVim()
    seed_host(conn, vms=("vm-1", "vm-2"))
    await _bootstrap(conn)
    requester = _person("op-requester")
    preview = await preview_operation(requester, _args(_REMOVE))
    assert preview["status"] == "ok", preview

    call = await call_operation(
        requester, {**_args(_REMOVE), "preview_hash": preview["preview_hash"]}
    )
    assert call["status"] == "awaiting_approval", call
    assert _sent(conn) == []
    request_id = UUID(call["extras"]["approval_request_id"])
    async with get_sessionmaker()() as s:
        row = await s.get(ApprovalRequest, request_id)
        assert row is not None
        effect = dict(row.proposed_effect)
    blast = effect["blast_radius"]
    assert blast["object"]["moid"] == HOST
    assert [child["moid"] for child in blast["children"]] == ["vm-1", "vm-2"]
    assert blast["irreversibility"] == "permanent"


@pytest.mark.usefixtures("_dispatch_env")
async def test_remove_is_refused_without_a_matching_preview_hash() -> None:
    conn = HostVim()
    seed_host(conn)
    await _bootstrap(conn)
    result = await dispatch(
        operator=_person("op-requester"),
        connector_id=_CONNECTOR_ID,
        op_id=_REMOVE,
        target=_TARGET_NAME,
        params={"host": HOST},
    )
    assert result.status == "denied", result
    assert result.extras["error_code"] == "preview_binding_required"
    call = await call_operation(
        _person("op-requester"), {**_args(_REMOVE), "preview_hash": "0" * 64}
    )
    assert call["status"] == "denied", call
    assert _sent(conn) == []
    assert await _approval_count() == 0


@pytest.mark.usefixtures("_dispatch_env")
async def test_remove_of_a_host_that_does_not_exist_does_not_wait() -> None:
    conn = HostVim()
    await _bootstrap(conn)
    requester = _person("op-requester")
    preview = await preview_operation(requester, _args(_REMOVE))
    call = await call_operation(
        requester, {**_args(_REMOVE), "preview_hash": preview["preview_hash"]}
    )
    assert call["status"] == "denied", call
    assert call["extras"]["error_code"] == "blast_radius_required"
    assert await _approval_count() == 0


@pytest.mark.usefixtures("_dispatch_env")
async def test_an_agent_cannot_remove_a_host() -> None:
    conn = HostVim()
    seed_host(conn)
    await _bootstrap(conn)
    result = await dispatch(
        operator=_person("agent-1", PrincipalKind.AGENT),
        connector_id=_CONNECTOR_ID,
        op_id=_REMOVE,
        target=_TARGET_NAME,
        params={"host": HOST},
    )
    assert result.status == "denied", result
    assert "destructive" in (result.error or "")
    assert _sent(conn) == []
