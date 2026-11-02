# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The host local-user and Active Directory operations through the real dispatcher.

* every operation is registered on both vCenter catalogs (8.0 and 9.0) with
  its safety level and approval flag: the two reads are ``safe`` without
  approval, every change waits for approval;
* a person's call of each change waits for a second person; nothing reaches
  the host before that, and the approval request holds the Vault path, never
  a password;
* ``host.local_user.delete`` is destructive: without a matching
  ``preview_hash`` it is refused, a user that does not exist is refused before
  it waits, the approval card shows the blast radius, and an agent is always
  refused.
"""

from __future__ import annotations

import json
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
from meho_backplane.connectors.vmware_rest.typed_ops import register_vmware_typed_operations
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import ApprovalRequest, EndpointDescriptor
from meho_backplane.db.models import Target as TargetORM
from meho_backplane.operations import dispatch, reset_dispatcher_caches
from meho_backplane.operations._handler_resolve import _CONNECTOR_INSTANCE_CACHE
from meho_backplane.operations.meta_tools import call_operation, preview_operation
from meho_backplane.settings import get_settings
from tests._vmware_host_access_fake import ESXI_HOST, HOST, PASSWORD, AccessVim

_CONNECTOR_ID = "vmware-rest-9.0"
_TENANT_ID = UUID("00000000-0000-0000-0000-000000004353")
_ESXI = "example-esxi"
_VCENTER = "example-vcenter"
_VAULT = "<vault-path-of-the-password>"

#: op id -> (safety level, requires approval, target, the params of one call)
_OPS: dict[str, tuple[str, bool, str, dict[str, Any]]] = {
    "vmware.host.local_user.list": ("safe", False, _ESXI, {}),
    "vmware.host.ad.status": ("safe", False, _VCENTER, {"host": HOST}),
    "vmware.composite.host.local_user.create": (
        "dangerous",
        True,
        _ESXI,
        {"user_name": "svc-new", "password_secret_ref": _VAULT},
    ),
    "vmware.composite.host.local_user.password.set": (
        "dangerous",
        True,
        _ESXI,
        {"user_name": "svc-old", "password_secret_ref": _VAULT},
    ),
    "vmware.composite.host.local_user.delete": (
        "destructive",
        True,
        _ESXI,
        {"user_name": "svc-old"},
    ),
    "vmware.composite.host.ad.join": (
        "dangerous",
        True,
        _VCENTER,
        {
            "host": HOST,
            "domain": "corp.example",
            "user_name": "<join-account>",
            "password_secret_ref": _VAULT,
        },
    ),
    "vmware.composite.host.ad.leave": ("dangerous", True, _VCENTER, {"host": HOST}),
}
_DELETE = "vmware.composite.host.local_user.delete"
_CHANGES = sorted(op for op, row in _OPS.items() if row[1] and op != _DELETE)
_VENDOR_WRITES = (
    "CreateUser",
    "UpdateUser",
    "RemoveUser",
    "JoinDomain_Task",
    "LeaveCurrentDomain_Task",
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
        name="Host Access Test",
        email=None,
        raw_jwt="<test-raw-jwt>",
        tenant_id=_TENANT_ID,
        tenant_role=TenantRole.OPERATOR,
        principal_kind=kind,
    )


def _target_row(name: str, fingerprint: dict[str, Any]) -> TargetORM:
    return TargetORM(
        id=uuid.uuid4(),
        tenant_id=_TENANT_ID,
        name=name,
        aliases=[],
        product="vmware",
        host=f"{name}.example.test",
        port=443,
        fqdn=None,
        secret_ref=None,
        auth_model="shared_service_account",
        vpn_required=False,
        extras={},
        fingerprint=fingerprint,
        preferred_impl_id="vmware-rest",
        notes="seeded by test_connectors_vmware_rest_host_access_approval",
    )


async def _bootstrap(conn: AccessVim) -> None:
    register_connector_v2(
        product="vmware", version="9.0", impl_id="vmware-rest", cls=VmwareRestConnector
    )
    _CONNECTOR_INSTANCE_CACHE[VmwareRestConnector] = conn  # type: ignore[assignment]
    await register_vmware_composite_operations(embedding_service=_embedding_stub())
    async with get_sessionmaker()() as s:
        s.add(_target_row(_VCENTER, {"version": "9.0"}))
        s.add(_target_row(_ESXI, {"version": "9.0", "product": "esxi", "reachable": True}))
        await s.commit()


def _conn() -> AccessVim:
    conn = AccessVim()
    conn.seed()
    conn.seed(ESXI_HOST)
    return conn


def _args(op_id: str) -> dict[str, Any]:
    _level, _approval, target, params = _OPS[op_id]
    return {"connector_id": _CONNECTOR_ID, "op_id": op_id, "target": target, "params": params}


def _sent(conn: AccessVim) -> list[str]:
    return [method for method in conn.methods() if method in _VENDOR_WRITES]


async def _approval_rows() -> list[ApprovalRequest]:
    async with get_sessionmaker()() as s:
        return list((await s.execute(select(ApprovalRequest))).scalars().all())


async def _approval_count() -> int:
    async with get_sessionmaker()() as s:
        return int(
            (await s.execute(select(func.count()).select_from(ApprovalRequest))).scalar_one()
        )


@pytest.mark.usefixtures("_dispatch_env")
async def test_each_operation_is_registered_with_its_level_on_both_catalogs() -> None:
    await register_vmware_composite_operations(embedding_service=_embedding_stub())
    await register_vmware_composite_operations_8_0(embedding_service=_embedding_stub())
    for version in ("9.0", "8.0"):
        await register_vmware_typed_operations(embedding_service=_embedding_stub(), version=version)
    async with get_sessionmaker()() as s:
        rows = (
            (await s.execute(select(EndpointDescriptor).where(EndpointDescriptor.op_id.in_(_OPS))))
            .scalars()
            .all()
        )
    seen = {(row.op_id, row.version) for row in rows}
    assert seen == {(op_id, version) for op_id in _OPS for version in ("8.0", "9.0")}
    for row in rows:
        level, approval, _target, _params = _OPS[row.op_id]
        assert row.safety_level == level, row.op_id
        assert row.requires_approval is approval, row.op_id


@pytest.mark.usefixtures("_dispatch_env")
@pytest.mark.parametrize("op_id", _CHANGES)
async def test_a_persons_call_waits_for_approval_and_sends_nothing(op_id: str) -> None:
    conn = _conn()
    await _bootstrap(conn)
    call = await call_operation(_person("op-requester"), _args(op_id))
    assert call["status"] == "awaiting_approval", call
    assert _sent(conn) == []
    [row] = await _approval_rows()
    stored = json.dumps({"params": row.params, "effect": row.proposed_effect}, default=str)
    assert PASSWORD not in stored


@pytest.mark.usefixtures("_dispatch_env")
async def test_delete_waits_for_approval_with_the_blast_radius() -> None:
    conn = _conn()
    conn.roles["svc-old"] = "accessReadOnly"
    await _bootstrap(conn)
    requester = _person("op-requester")
    preview = await preview_operation(requester, _args(_DELETE))
    assert preview["status"] == "ok", preview
    call = await call_operation(
        requester, {**_args(_DELETE), "preview_hash": preview["preview_hash"]}
    )
    assert call["status"] == "awaiting_approval", call
    assert _sent(conn) == []
    [row] = await _approval_rows()
    blast = dict(row.proposed_effect)["blast_radius"]
    assert blast["object"]["user"] == "svc-old"
    assert blast["children"] == [
        {"kind": "access_role", "user": "svc-old", "access_mode": "accessReadOnly"}
    ]
    assert blast["irreversibility"] == "permanent"


@pytest.mark.usefixtures("_dispatch_env")
async def test_delete_is_refused_without_a_matching_preview_hash() -> None:
    conn = _conn()
    await _bootstrap(conn)
    result = await dispatch(
        operator=_person("op-requester"),
        connector_id=_CONNECTOR_ID,
        op_id=_DELETE,
        target=_ESXI,
        params={"user_name": "svc-old"},
    )
    assert result.status == "denied", result
    assert result.extras["error_code"] == "preview_binding_required"
    call = await call_operation(
        _person("op-requester"), {**_args(_DELETE), "preview_hash": "0" * 64}
    )
    assert call["status"] == "denied", call
    assert _sent(conn) == []
    assert await _approval_count() == 0


@pytest.mark.usefixtures("_dispatch_env")
async def test_delete_of_a_user_that_does_not_exist_does_not_wait() -> None:
    conn = _conn()
    conn.users.pop("svc-old")
    await _bootstrap(conn)
    requester = _person("op-requester")
    preview = await preview_operation(requester, _args(_DELETE))
    call = await call_operation(
        requester, {**_args(_DELETE), "preview_hash": preview["preview_hash"]}
    )
    assert call["status"] == "denied", call
    assert call["extras"]["error_code"] == "blast_radius_required"
    assert await _approval_count() == 0


@pytest.mark.usefixtures("_dispatch_env")
async def test_an_agent_cannot_delete_a_local_user() -> None:
    conn = _conn()
    await _bootstrap(conn)
    result = await dispatch(
        operator=_person("agent-1", PrincipalKind.AGENT),
        connector_id=_CONNECTOR_ID,
        op_id=_DELETE,
        target=_ESXI,
        params={"user_name": "svc-old"},
    )
    assert result.status == "denied", result
    assert "destructive" in (result.error or "")
    assert _sent(conn) == []
