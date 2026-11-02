# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""``vm.migrate_cross_vcenter`` through the dispatcher and over HTTP.

* It registers on the ``vmware-rest-8.0`` and ``vmware-rest-9.0`` catalogs as
  a ``dangerous`` change that waits for approval.
* A person's call waits until a second person approves; then the move starts
  once. The other vCenter is a real target row, found by name. An agent
  without a grant is refused.
* The status read the answer names (``vmware.task.info`` with the task id)
  works on both catalogs.
* The real connector logs in to the other vCenter with the login from Vault
  on its own client, never with a cached session, and sends the VIM calls on
  the vCenter 8 path (``/sdk/vim25/8.0.3.0/...``) and the vCenter 9 path
  (``/sdk/vim25/9.0.0.0/...``). A refused login there stops the move. The
  certificate check of that client is in
  ``test_connectors_vmware_rest_vm_cross_vcenter_tls.py``.
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
from meho_backplane.connectors._shared.cache_key import target_cache_key
from meho_backplane.connectors.registry import clear_registry, register_connector_v2
from meho_backplane.connectors.vmware_rest import VmwareRest80Connector, VmwareRestConnector
from meho_backplane.connectors.vmware_rest.composites import (
    _vm_cross_vcenter,
    _write,
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
from meho_backplane.operations import dispatch, reset_dispatcher_caches
from meho_backplane.operations._handler_resolve import _CONNECTOR_INSTANCE_CACHE
from meho_backplane.operations.approval_queue import (
    approve_request,
    resume_dispatch_after_approval,
)
from meho_backplane.operations.meta_tools import call_operation
from meho_backplane.settings import get_settings
from tests._vmware_cross_vcenter_fake import (
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
_CONNECTOR_ID_8_0 = "vmware-rest-8.0"
#: A second source vCenter on version 8.0.3, served by the 8.0 catalog.
_SOURCE_8_0_NAME = "example-vcenter-c"
_TENANT_ID = UUID("00000000-0000-0000-0000-000000004347")


@pytest.fixture
def _dispatch_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    reset_dispatcher_caches()
    clear_registry()

    async def _open(connector: CrossVimFake, dest: Any, op: Operator, thumbprint: str) -> Any:
        return await connector.open_destination(connector, dest, op, thumbprint)

    monkeypatch.setattr(_vm_cross_vcenter, "open_destination", _open)
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


def _target_row(name: str, host: str, extras: dict[str, Any], *, version: str = "9.0") -> TargetORM:
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
        fingerprint={"version": version},
        preferred_impl_id="vmware-rest",
        notes="seeded by test_connectors_vmware_rest_vm_cross_vcenter_dispatch",
    )


async def _bootstrap(conn: CrossVimFake) -> None:
    register_connector_v2(
        product="vmware", version="9.0", impl_id="vmware-rest", cls=VmwareRestConnector
    )
    register_connector_v2(
        product="vmware", version="8.0", impl_id="vmware-rest", cls=VmwareRest80Connector
    )
    _CONNECTOR_INSTANCE_CACHE[VmwareRestConnector] = conn  # type: ignore[assignment]
    _CONNECTOR_INSTANCE_CACHE[VmwareRest80Connector] = conn  # type: ignore[assignment]
    await register_vmware_composite_operations(embedding_service=_embedding_stub())
    await register_vmware_composite_operations_8_0(embedding_service=_embedding_stub())
    await register_vmware_typed_operations(embedding_service=_embedding_stub())
    await register_vmware_typed_operations_8_0(embedding_service=_embedding_stub())
    async with get_sessionmaker()() as s:
        s.add(_target_row(SOURCE_NAME, "vc-a.example.test", {}))
        s.add(_target_row(_SOURCE_8_0_NAME, "vc-c.example.test", {}, version="8.0.3"))
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
@pytest.mark.parametrize(
    ("connector_id", "source_name", "about", "release"),
    [
        (_CONNECTOR_ID, SOURCE_NAME, "9.0.0", "9.0.0.0"),
        (_CONNECTOR_ID_8_0, _SOURCE_8_0_NAME, "8.0.3", "8.0.3.0"),
    ],
)
async def test_the_status_read_of_the_answer_works_on_each_catalog(
    monkeypatch: pytest.MonkeyPatch,
    connector_id: str,
    source_name: str,
    about: str,
    release: str,
) -> None:
    """``status_read`` names a registered op; its params read the task on each catalog."""
    monkeypatch.setattr(_write, "enforce_subop_policy", GateRecorder())
    conn = CrossVimFake()
    seed(conn)
    await _bootstrap(conn)
    async with get_sessionmaker()() as s:
        source = (
            await s.execute(select(TargetORM).where(TargetORM.name == source_name))
        ).scalar_one()
    out = await _vm_cross_vcenter.vm_migrate_cross_vcenter_composite(
        operator=_person("op-status"), target=source, params=params(), connector=conn
    )
    assert out["status"] == "accepted", out
    status_read = out["status_read"]

    # The status read runs on the real connector of each catalog, over HTTP.
    real: dict[type[Any], Any] = {
        VmwareRestConnector: VmwareRestConnector(session_loader=_stub_loader),
        VmwareRest80Connector: VmwareRest80Connector(session_loader=_stub_loader),
    }
    _CONNECTOR_INSTANCE_CACHE.update(real)
    base = f"https://{source.host}"
    task_info = {"state": "running", "descriptionId": "VirtualMachine.relocate", "progress": 40}
    try:
        async with respx.mock(assert_all_called=False) as mock:
            mock.post(f"{base}/api/session").respond(200, json="tok")
            mock.get(f"{base}/api/about").respond(200, json={"version": about})
            mock.post(
                f"{base}/sdk/vim25/{release}/PropertyCollector/propertyCollector/"
                "RetrievePropertiesEx"
            ).mock(side_effect=_retrieve({("Task", out["task"]): {"info": task_info}}))
            read = await call_operation(
                _person("op-status"),
                {
                    "connector_id": connector_id,
                    "op_id": status_read["op_id"],
                    "target": source_name,
                    "params": status_read["params"],
                },
            )
    finally:
        for connector in real.values():
            for client in connector._clients.values():
                await client.aclose()
    assert read["status"] == "ok", read
    assert read["result"]["task"] == out["task"]
    assert read["result"]["state"] == "running"
    assert read["result"]["progress"] == 40


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


def _versions_xml(release: str) -> str:
    return (
        '<namespaces version="1.0"><namespace><name>urn:vim25</name>'
        f"<version>{release}</version></namespace></namespaces>"
    )


class _OverHttp:
    """The answer of one move over HTTP, and the routes of the other vCenter."""

    def __init__(self, out: dict[str, Any], start: respx.Route, dest_login: respx.Route) -> None:
        self.out = out
        self.start = start
        self.dest_login = dest_login


async def _over_http(
    about: str, release: str, *, dest_login: int = 200, cached_session: str | None = None
) -> _OverHttp:
    connector = VmwareRestConnector(session_loader=_stub_loader)
    destination = dest_target()
    if cached_session is not None:
        connector._session_tokens[target_cache_key(destination)] = cached_session

    async def _resolve(_operator: Any, _name: str) -> Target:
        return destination

    try:
        async with respx.mock(assert_all_called=False) as mock:
            mock.get(f"{_SOURCE}/api/about").respond(200, json={"version": about})
            mock.post(f"{_SOURCE}/api/session").respond(200, json="tok-a")
            login = mock.post(f"{_DEST}/api/session").respond(dest_login, json="tok-b")
            mock.delete(f"{_DEST}/api/session").respond(204)
            mock.get(f"{_DEST}/sdk/vimServiceVersions.xml").respond(
                200, text=_versions_xml(release)
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
            if cached_session is not None:
                for call in mock.calls:
                    if call.request.url.host == "vc-b.example.test":
                        assert call.request.headers.get("vmware-api-session-id") != cached_session
        return _OverHttp(out, start, login)
    finally:
        connector._session_tokens.clear()
        for client in connector._clients.values():
            await client.aclose()
        connector._clients.clear()


def _basic_auth(login: dict[str, str]) -> str:
    return httpx.BasicAuth(login["username"], login["password"])._auth_header


@pytest.mark.parametrize(("about", "release"), [("8.0.3", "8.0.3.0"), ("9.0.0", "9.0.0.0")])
async def test_the_move_starts_on_the_vi_json_path_of_each_version(
    monkeypatch: pytest.MonkeyPatch, about: str, release: str
) -> None:
    monkeypatch.setattr(_write, "enforce_subop_policy", GateRecorder())
    run = await _over_http(about, release)
    assert run.out["status"] == "accepted", run.out
    assert run.out["task"] == "task-31"
    assert run.start.call_count == 1
    spec = json.loads(run.start.calls.last.request.content)["spec"]
    assert spec["service"]["url"] == _DEST
    assert spec["service"]["instanceUuid"] == DEST_UUID.upper()
    assert spec["service"]["sslThumbprint"] == THUMBPRINT
    assert spec["service"]["credential"]["username"] == LOGIN["username"]
    assert spec["deviceChange"][0]["device"]["backing"]["port"]["switchUuid"] == (
        "dest-switch-uuid"
    )
    # The login that was tested on the other vCenter is the one in the locator.
    assert run.dest_login.call_count == 1
    assert run.dest_login.calls.last.request.headers["authorization"] == _basic_auth(LOGIN)


async def test_a_refused_login_on_the_other_vcenter_stops_the_move(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(_write, "enforce_subop_policy", GateRecorder())
    run = await _over_http("9.0.0", "9.0.0.0", dest_login=401)
    assert run.out["status"] == "destination_login_failed", run.out
    assert run.start.call_count == 0


async def test_a_cached_session_does_not_stand_in_for_the_vault_login(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A valid cached session of the other vCenter, but the Vault login is wrong: no move."""
    monkeypatch.setattr(_write, "enforce_subop_policy", GateRecorder())
    run = await _over_http("9.0.0", "9.0.0.0", dest_login=401, cached_session="tok-cached")
    assert run.out["status"] == "destination_login_failed", run.out
    assert run.start.call_count == 0
    assert run.dest_login.call_count == 1
    assert run.dest_login.calls.last.request.headers["authorization"] == _basic_auth(LOGIN)
