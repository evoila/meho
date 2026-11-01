# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""``vm.migrate_cross_vcenter`` through the dispatcher and over HTTP.

* It registers on the ``vmware-rest-8.0`` and ``vmware-rest-9.0`` catalogs as
  a ``dangerous`` change that waits for approval.
* A person's call waits until a second person approves; then the move starts
  once. The other vCenter is a real target row, found by name. An agent
  without a grant is refused.
* The real connector reads the other vCenter's certificate on its own TLS
  connection, logs in there, and sends the VIM calls on the vCenter 8 path
  (``/sdk/vim25/8.0.3.0/...``) and the vCenter 9 path
  (``/sdk/vim25/9.0.0.0/...``). A failed login there stops the move.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable, Iterator
from typing import Any
from unittest.mock import AsyncMock
from uuid import UUID

import httpx
import pytest
import respx
from sqlalchemy import func, select

from meho_backplane.auth.operator import Operator, PrincipalKind, TenantRole
from meho_backplane.connectors.registry import clear_registry, register_connector_v2
from meho_backplane.connectors.vmware_rest import VmwareRestConnector
from meho_backplane.connectors.vmware_rest.composites import (
    _vm_cross_vcenter,
    _write,
    register_vmware_composite_operations,
    register_vmware_composite_operations_8_0,
)
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import ApprovalRequest, EndpointDescriptor
from meho_backplane.db.models import Target as TargetORM
from meho_backplane.operations import dispatch, reset_dispatcher_caches
from meho_backplane.operations._handler_resolve import _CONNECTOR_INSTANCE_CACHE
from meho_backplane.operations.approval_queue import (
    approve_request,
    resume_dispatch_after_approval,
)
from meho_backplane.operations.meta_tools import call_operation
from meho_backplane.settings import get_settings
from tests._vmware_cross_vcenter_fake import (
    CERT,
    DEST_NAME,
    DEST_UUID,
    LOGIN,
    SOURCE_NAME,
    THUMBPRINT,
    VM,
    CrossVimFake,
    Target,
    dest_target,
    nic,
    params,
    seed,
    source_target,
)
from tests._vmware_teardown_fake import GateRecorder

_OP = "vmware.composite.vm.migrate_cross_vcenter"
_CONNECTOR_ID = "vmware-rest-9.0"
_TENANT_ID = UUID("00000000-0000-0000-0000-000000004347")


@pytest.fixture
def _dispatch_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    reset_dispatcher_caches()
    clear_registry()

    async def _cert(_connector: Any, _target: Any) -> bytes:
        return CERT

    monkeypatch.setattr(_vm_cross_vcenter, "presented_certificate", _cert)
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
        name="Cross vCenter Test",
        email=None,
        raw_jwt="<test-raw-jwt>",
        tenant_id=_TENANT_ID,
        tenant_role=TenantRole.OPERATOR,
        principal_kind=kind,
    )


def _target_row(name: str, host: str, extras: dict[str, Any]) -> TargetORM:
    return TargetORM(
        id=uuid.uuid4(),
        tenant_id=_TENANT_ID,
        name=name,
        aliases=[],
        product="vmware",
        host=host,
        port=443,
        fqdn=None,
        secret_ref=f"vsphere/{name}",
        auth_model="shared_service_account",
        vpn_required=False,
        extras=extras,
        fingerprint={"version": "9.0"},
        preferred_impl_id="vmware-rest",
        notes="seeded by test_connectors_vmware_rest_vm_cross_vcenter_dispatch",
    )


async def _bootstrap(conn: CrossVimFake) -> None:
    register_connector_v2(
        product="vmware", version="9.0", impl_id="vmware-rest", cls=VmwareRestConnector
    )
    _CONNECTOR_INSTANCE_CACHE[VmwareRestConnector] = conn  # type: ignore[assignment]
    await register_vmware_composite_operations(embedding_service=_embedding_stub())
    await register_vmware_composite_operations_8_0(embedding_service=_embedding_stub())
    async with get_sessionmaker()() as s:
        s.add(_target_row(SOURCE_NAME, "vc-a.example.test", {}))
        s.add(_target_row(DEST_NAME, "vc-b.example.test", {"ssl_thumbprint": THUMBPRINT}))
        await s.commit()


async def _approve_and_resume(request_id: UUID) -> Any:
    approver = _person("op-approver")
    async with get_sessionmaker()() as s:
        approved = await approve_request(s, request_id, operator=approver, params=None)
        await s.commit()
    assert approved.status == "approved"
    async with get_sessionmaker()() as s:
        row = await s.get(ApprovalRequest, request_id)
        assert row is not None
        return await resume_dispatch_after_approval(operator=approver, request=row, params=None)


@pytest.mark.usefixtures("_dispatch_env")
async def test_it_registers_as_a_dangerous_change_on_both_catalogs() -> None:
    await _bootstrap(CrossVimFake())
    async with get_sessionmaker()() as s:
        rows = (
            (await s.execute(select(EndpointDescriptor).where(EndpointDescriptor.op_id == _OP)))
            .scalars()
            .all()
        )
    assert {row.version for row in rows} == {"8.0", "9.0"}
    for row in rows:
        assert row.safety_level == "dangerous"
        assert row.requires_approval is True


@pytest.mark.usefixtures("_dispatch_env")
async def test_a_person_waits_for_a_second_person_then_the_move_starts_once() -> None:
    conn = CrossVimFake()
    seed(conn)
    await _bootstrap(conn)
    call = await call_operation(
        _person("op-requester"),
        {"connector_id": _CONNECTOR_ID, "op_id": _OP, "target": SOURCE_NAME, "params": params()},
    )
    assert call["status"] == "awaiting_approval", call
    assert conn.starts() == []
    async with get_sessionmaker()() as s:
        row = await s.get(ApprovalRequest, UUID(call["extras"]["approval_request_id"]))
        assert row is not None
        assert LOGIN["password"] not in json.dumps(row.proposed_effect, default=str)

    resume = await _approve_and_resume(UUID(call["extras"]["approval_request_id"]))
    assert resume.status == "ok", resume.error
    assert resume.result["status"] == "accepted"
    assert resume.result["status_read"]["op_id"] == "vmware.task.info"
    assert len(conn.starts()) == 1
    assert LOGIN["password"] not in json.dumps(resume.result)


@pytest.mark.usefixtures("_dispatch_env")
async def test_an_agent_without_a_grant_is_refused() -> None:
    conn = CrossVimFake()
    seed(conn)
    await _bootstrap(conn)
    async with get_sessionmaker()() as s:
        source = (
            await s.execute(select(TargetORM).where(TargetORM.name == SOURCE_NAME))
        ).scalar_one()
    result = await dispatch(
        operator=_person("agent-1", PrincipalKind.AGENT),
        connector_id=_CONNECTOR_ID,
        op_id=_OP,
        target=source,
        params=params(),
    )
    assert result.status == "denied", result
    assert conn.starts() == []
    async with get_sessionmaker()() as s:
        count = (await s.execute(select(func.count()).select_from(ApprovalRequest))).scalar_one()
    assert count == 0


# ===========================================================================
# Over HTTP: the real connector, two vCenters
# ===========================================================================

_SOURCE = "https://vc-a.example.test"
_DEST = "https://vc-b.example.test"


class _TlsStream:
    """The ``network_stream`` extension of an answer: the TLS connection's certificate."""

    def __init__(self, cert: bytes) -> None:
        self._cert = cert

    def get_extra_info(self, info: str) -> Any:
        if info != "ssl_object":
            return None
        cert = self._cert

        class _Ssl:
            def getpeercert(self, binary_form: bool = False) -> bytes:
                return cert

        return _Ssl()


def _retrieve(objects: dict[tuple[str, str], dict[str, Any]]) -> Callable[..., httpx.Response]:
    """A ``RetrievePropertiesEx`` answer from *objects*; an unknown object is left out."""

    def _answer(request: httpx.Request) -> httpx.Response:
        spec = json.loads(request.content)["specSet"][0]
        mo_type = spec["propSet"][0]["type"]
        paths = spec["propSet"][0]["pathSet"]
        found = []
        for entry in spec["objectSet"]:
            moid = entry["obj"]["value"]
            props = objects.get((mo_type, moid))
            if props is None:
                continue
            found.append(
                {
                    "_typeName": "ObjectContent",
                    "obj": {"_typeName": "ManagedObjectReference", "type": mo_type, "value": moid},
                    "propSet": [
                        {"_typeName": "DynamicProperty", "name": p, "val": props[p]}
                        for p in paths
                        if p in props
                    ],
                }
            )
        return httpx.Response(200, json={"_typeName": "RetrieveResult", "objects": found})

    return _answer


def _ref(mo_type: str, moid: str) -> dict[str, str]:
    return {"_typeName": "ManagedObjectReference", "type": mo_type, "value": moid}


_SOURCE_OBJECTS: dict[tuple[str, str], dict[str, Any]] = {
    ("VirtualMachine", VM): {
        "name": "<vm-name>",
        "config.hardware.device": {"_typeName": "ArrayOfVirtualDevice", "_value": [nic(4000)]},
        "recentTask": {"_typeName": "ArrayOfManagedObjectReference", "_value": []},
    },
}
_DEST_OBJECTS: dict[tuple[str, str], dict[str, Any]] = {
    ("HostSystem", "host-21"): {"name": "<host-name>"},
    ("ResourcePool", "resgroup-8"): {"name": "Resources"},
    ("Datastore", "datastore-12"): {"name": "<datastore-name>"},
    ("Folder", "group-v3"): {"name": "vm"},
    ("DistributedVirtualPortgroup", "dvportgroup-34"): {
        "key": "dvportgroup-34",
        "config.distributedVirtualSwitch": _ref("VmwareDistributedVirtualSwitch", "dvs-7"),
    },
    ("VmwareDistributedVirtualSwitch", "dvs-7"): {"uuid": "dest-switch-uuid"},
}


async def _stub_loader(_target: Any, _operator: Operator) -> dict[str, str]:
    return dict(LOGIN)


async def _over_http(
    about: str, release: str, *, dest_login: int = 200
) -> tuple[dict[str, Any], respx.Route]:
    connector = VmwareRestConnector(session_loader=_stub_loader)
    destination = dest_target()

    async def _resolve(_operator: Any, _name: str) -> Target:
        return destination

    try:
        async with respx.mock(assert_all_called=False) as mock:
            for base in (_SOURCE, _DEST):
                mock.get(f"{base}/api/about").respond(200, json={"version": about})
            mock.post(f"{_SOURCE}/api/session").respond(200, json="tok-a")
            mock.post(f"{_DEST}/api/session").respond(dest_login, json="tok-b")
            mock.get(f"{_DEST}/sdk/vimServiceVersions.xml").mock(
                side_effect=lambda _r: httpx.Response(
                    200, text="<x/>", extensions={"network_stream": _TlsStream(CERT)}
                )
            )
            read = "PropertyCollector/propertyCollector/RetrievePropertiesEx"
            mock.post(f"{_SOURCE}/sdk/vim25/{release}/{read}").mock(
                side_effect=_retrieve(_SOURCE_OBJECTS)
            )
            mock.post(f"{_DEST}/sdk/vim25/{release}/{read}").mock(
                side_effect=_retrieve(_DEST_OBJECTS)
            )
            mock.post(
                f"{_DEST}/sdk/vim25/{release}/ServiceInstance/ServiceInstance/RetrieveServiceContent"
            ).respond(
                200,
                json={"_typeName": "ServiceContent", "about": {"instanceUuid": DEST_UUID}},
            )
            start = mock.post(f"{_SOURCE}/sdk/vim25/{release}/VirtualMachine/{VM}/RelocateVM_Task")
            start.respond(200, json=_ref("Task", "task-31"))
            with pytest.MonkeyPatch.context() as mp:
                mp.setattr(_vm_cross_vcenter, "resolve_destination", _resolve)
                out = await _vm_cross_vcenter.vm_migrate_cross_vcenter_composite(
                    operator=_person("op-http"),
                    target=source_target(),
                    params=params(),
                    connector=connector,
                )
        return out, start
    finally:
        connector._session_tokens.clear()
        for client in connector._clients.values():
            await client.aclose()
        connector._clients.clear()


@pytest.mark.parametrize(("about", "release"), [("8.0.3", "8.0.3.0"), ("9.0.0", "9.0.0.0")])
async def test_the_move_starts_on_the_vi_json_path_of_each_version(
    monkeypatch: pytest.MonkeyPatch, about: str, release: str
) -> None:
    monkeypatch.setattr(_write, "enforce_subop_policy", GateRecorder())
    out, start = await _over_http(about, release)
    assert out["status"] == "accepted", out
    assert out["task"] == "task-31"
    assert start.call_count == 1
    spec = json.loads(start.calls.last.request.content)["spec"]
    assert spec["service"]["url"] == _DEST
    assert spec["service"]["instanceUuid"] == DEST_UUID.upper()
    assert spec["service"]["sslThumbprint"] == THUMBPRINT
    assert spec["deviceChange"][0]["device"]["backing"]["port"]["switchUuid"] == (
        "dest-switch-uuid"
    )


async def test_a_failed_login_on_the_other_vcenter_stops_the_move(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(_write, "enforce_subop_policy", GateRecorder())
    out, start = await _over_http("9.0.0", "9.0.0.0", dest_login=401)
    assert out["status"] == "destination_login_failed", out
    assert start.call_count == 0


async def test_the_certificate_is_read_from_the_tls_connection() -> None:
    connector = VmwareRestConnector(session_loader=_stub_loader)
    try:
        async with respx.mock() as mock:
            mock.get(f"{_DEST}/sdk/vimServiceVersions.xml").mock(
                side_effect=lambda _r: httpx.Response(
                    200, extensions={"network_stream": _TlsStream(CERT)}
                )
            )
            assert await _vm_cross_vcenter.presented_certificate(connector, dest_target()) == CERT
    finally:
        for client in connector._clients.values():
            await client.aclose()
        connector._clients.clear()
