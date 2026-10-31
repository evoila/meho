# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Host local users and Active Directory over HTTP: vCenter 8 and 9, and a single host.

The real :class:`VmwareRestConnector` runs against ``respx``. The vendor
answers are written by hand from the API reference, with made-up names, ids
and passwords.

* On a vCenter, the domain join and the status read go to
  ``/sdk/vim25/8.0.3.0/...`` (vCenter 8.0 U3) and ``/sdk/vim25/9.0.0.0/...``
  (vCenter 9.0) as VI-JSON. The join polls its task and reads the host back.
* On a single ESXi host, every call is SOAP on ``POST /sdk``: the user list
  with the access roles, the user create (the envelope carries the
  ``HostAccountSpec``), a SOAP fault of the create (its type and text reach the
  answer, the password never does), the user delete, and the domain join.
"""

from __future__ import annotations

import html
import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
import respx
from defusedxml.ElementTree import fromstring

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors.schemas import AuthModel
from meho_backplane.connectors.vmware_rest import VmwareRestConnector, VsphereTargetLike
from meho_backplane.connectors.vmware_rest import typed_ops_host_access as typed
from meho_backplane.connectors.vmware_rest.composites import _host_ad, _host_local_users, _write
from tests._vmware_teardown_fake import GateRecorder

_VC_BASE = "https://vc.test.invalid"
_ESXI_BASE = "https://esxi.test.invalid"
_HOST = "host-1001"
_RELEASES = {"8.0.3": "8.0.3.0", "9.0.0.0": "9.0.0.0"}
_PASSWORD = "<password-from-vault>"
_VAULT = "<vault-path-of-the-password>"


@dataclass
class _Target:
    name: str = "example-vcenter"
    host: str = "vc.test.invalid"
    port: int | None = 443
    secret_ref: str = "vsphere/example"
    auth_model: str | None = AuthModel.SHARED_SERVICE_ACCOUNT.value
    id: UUID = field(default_factory=uuid4)
    tenant_id: UUID = field(default_factory=lambda: UUID(int=0))
    fingerprint: dict[str, Any] | None = None


def _esxi_target() -> _Target:
    return _Target(
        name="example-esxi",
        host="esxi.test.invalid",
        secret_ref="vsphere/example-esxi",
        fingerprint={"product": "esxi", "reachable": True, "version": "8.0.3"},
    )


def _operator() -> Operator:
    return Operator(
        sub="op-host-access-http",
        name="Host Access HTTP Test",
        email=None,
        raw_jwt="<test-raw-jwt>",
        tenant_id=UUID("00000000-0000-0000-0000-000000004353"),
        tenant_role=TenantRole.OPERATOR,
    )


async def _loader(_target: VsphereTargetLike, _operator: Operator) -> dict[str, str]:
    return {"username": "<service-account>", "password": "<service-password>"}


def _connector() -> VmwareRestConnector:
    return VmwareRestConnector(session_loader=_loader)


async def _close(connector: VmwareRestConnector) -> None:
    for client in connector._clients.values():
        await client.aclose()
    connector._clients.clear()


@pytest.fixture
def gate(monkeypatch: pytest.MonkeyPatch) -> GateRecorder:
    recorder = GateRecorder()
    monkeypatch.setattr(_write, "enforce_subop_policy", recorder)
    monkeypatch.setattr(_host_ad, "_TASK_TIMEOUT_SECONDS", 0.0)

    async def _secret(*_a: Any, **_k: Any) -> str:
        return _PASSWORD

    monkeypatch.setattr(_host_local_users, "read_secret_param", _secret)
    monkeypatch.setattr(_host_ad, "read_secret_param", _secret)
    return recorder


# ===========================================================================
# vCenter: VI-JSON
# ===========================================================================


def _moref(mo_type: str, moid: str) -> dict[str, str]:
    return {"_typeName": "ManagedObjectReference", "type": mo_type, "value": moid}


def _object(mo_type: str, moid: str, props: dict[str, Any]) -> dict[str, Any]:
    return {
        "objects": [
            {
                "obj": _moref(mo_type, moid),
                "propSet": [{"name": name, "val": val} for name, val in props.items()],
            }
        ]
    }


def _vc_answers(state: dict[str, Any]) -> dict[str, Callable[[], dict[str, Any]]]:
    def host() -> dict[str, Any]:
        return _object(
            "HostSystem",
            _HOST,
            {
                "configManager.authenticationManager": _moref(
                    "HostAuthenticationManager", "authManager-1"
                )
            },
        )

    def manager() -> dict[str, Any]:
        ad: dict[str, Any] = {"_typeName": "HostActiveDirectoryInfo", "enabled": state["joined"]}
        if state["joined"]:
            ad.update(joinedDomain="CORP.EXAMPLE", domainMembershipStatus="ok")
        return _object(
            "HostAuthenticationManager",
            "authManager-1",
            {
                "info": {
                    "_typeName": "HostAuthenticationManagerInfo",
                    "authConfig": [
                        {"_typeName": "HostLocalAuthenticationInfo", "enabled": True},
                        ad,
                    ],
                },
                "supportedStore": {
                    "_typeName": "ArrayOfManagedObjectReference",
                    "_value": [
                        _moref("HostLocalAuthentication", "localAuth-1"),
                        _moref("HostActiveDirectoryAuthentication", "adAuth-1"),
                    ],
                },
            },
        )

    def task() -> dict[str, Any]:
        state["joined"] = True
        return _object("Task", "task-7", {"info": {"_typeName": "TaskInfo", "state": "success"}})

    return {"HostSystem": host, "HostAuthenticationManager": manager, "Task": task}


def _retrieve_responder(answers: dict[str, Callable[[], dict[str, Any]]]) -> Any:
    def respond(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        return httpx.Response(200, json=answers[body["specSet"][0]["propSet"][0]["type"]]())

    return respond


def _mock_vcenter(mock: respx.MockRouter, about: str, state: dict[str, Any]) -> str:
    mock.get("/api/about").respond(200, json={"version": about})
    mock.post("/api/session").respond(200, json="tok")
    mock.get("/api/vcenter/host").respond(200, json=[{"host": _HOST, "name": "<esxi-host-name>"}])
    base = f"/sdk/vim25/{_RELEASES[about]}"
    mock.post(f"{base}/PropertyCollector/propertyCollector/RetrievePropertiesEx").mock(
        side_effect=_retrieve_responder(_vc_answers(state))
    )
    return base


@pytest.mark.parametrize("about", sorted(_RELEASES))
async def test_join_runs_on_the_vcenter_path_of_each_version(
    gate: GateRecorder, about: str
) -> None:
    state = {"joined": False}
    connector = _connector()
    try:
        async with respx.mock(base_url=_VC_BASE) as mock:
            base = _mock_vcenter(mock, about, state)
            join = mock.post(
                f"{base}/HostActiveDirectoryAuthentication/adAuth-1/JoinDomain_Task"
            ).respond(200, json=_moref("Task", "task-7"))
            out = await _host_ad.ad_join_composite(
                operator=_operator(),
                target=_Target(),
                params={
                    "host": "<esxi-host-name>",
                    "domain": "corp.example",
                    "user_name": "<join-account>",
                    "password_secret_ref": _VAULT,
                },
                connector=connector,
            )
        assert join.called
        assert json.loads(join.calls.last.request.content) == {
            "domainName": "corp.example",
            "userName": "<join-account>",
            "password": _PASSWORD,
        }
        assert out["status"] == "joined"
        assert out["domain"] == "CORP.EXAMPLE"
        assert out["task"] == "task-7"
        assert _PASSWORD not in json.dumps(out)
    finally:
        await _close(connector)


@pytest.mark.parametrize("about", sorted(_RELEASES))
async def test_ad_status_reads_the_vcenter_path_of_each_version(about: str) -> None:
    connector = _connector()
    try:
        async with respx.mock(base_url=_VC_BASE) as mock:
            _mock_vcenter(mock, about, {"joined": True})
            out = await typed.host_ad_status_impl(
                connector, _operator(), _Target(), {"host": "<esxi-host-name>"}
            )
        assert out["status"] == "ok"
        assert out["joined"] is True
        assert out["domain"] == "CORP.EXAMPLE"
        assert out["membership_status"] == "ok"
    finally:
        await _close(connector)


# ===========================================================================
# A single ESXi host: SOAP
# ===========================================================================

_API_VERSION = "8.0.3.0"


def _soap(inner: str) -> str:
    return (
        '<soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/"'
        ' xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">'
        f"<soapenv:Body>{inner}</soapenv:Body></soapenv:Envelope>"
    )


def _response(method: str, inner: str = "") -> str:
    return _soap(f'<{method}Response xmlns="urn:vim25">{inner}</{method}Response>')


_SERVICE_CONTENT = _response(
    "RetrieveServiceContent",
    "<returnval>"
    '<propertyCollector type="PropertyCollector">ha-property-collector</propertyCollector>'
    '<sessionManager type="SessionManager">ha-sessionmgr</sessionManager>'
    '<accountManager type="HostLocalAccountManager">ha-localacctmgr</accountManager>'
    '<userDirectory type="UserDirectory">ha-user-directory</userDirectory>'
    f"<about><version>8.0.3</version><apiVersion>{_API_VERSION}</apiVersion>"
    "<apiType>HostAgent</apiType></about>"
    "</returnval>",
)

_USERS = _response(
    "RetrieveUserGroups",
    '<returnval xsi:type="PosixUserSearchResult"><principal>root</principal>'
    "<fullName>Administrator</fullName><group>false</group><id>0</id>"
    "<shellAccess>true</shellAccess></returnval>"
    '<returnval xsi:type="PosixUserSearchResult"><principal>svc-old</principal>'
    "<group>false</group><id>1001</id><shellAccess>false</shellAccess></returnval>",
)

_ACCESS = _response(
    "RetrieveHostAccessControlEntries",
    "<returnval><principal>root</principal><group>false</group>"
    "<accessMode>accessAdmin</accessMode></returnval>"
    "<returnval><principal>DOMAIN\\admins</principal><group>true</group>"
    "<accessMode>accessAdmin</accessMode></returnval>",
)


def _props(mo_type: str, moid: str, props: str) -> str:
    return _response(
        "RetrievePropertiesEx",
        f'<returnval><objects><obj type="{mo_type}">{moid}</obj>{props}</objects></returnval>',
    )


def _prop(name: str, val: str) -> str:
    return f"<propSet><name>{name}</name>{val}</propSet>"


def _ad_props(joined: bool) -> str:
    ad = (
        "<enabled>true</enabled><joinedDomain>CORP.EXAMPLE</joinedDomain>"
        if joined
        else ("<enabled>false</enabled>")
    )
    return _props(
        "HostAuthenticationManager",
        "ha-auth-manager",
        _prop(
            "info",
            '<val xsi:type="HostAuthenticationManagerInfo">'
            '<authConfig xsi:type="HostLocalAuthenticationInfo"><enabled>true</enabled>'
            "</authConfig>"
            f'<authConfig xsi:type="HostActiveDirectoryInfo">{ad}</authConfig></val>',
        )
        + _prop(
            "supportedStore",
            '<val xsi:type="ArrayOfManagedObjectReference">'
            '<ManagedObjectReference type="HostLocalAuthentication">ha-local-auth'
            "</ManagedObjectReference>"
            '<ManagedObjectReference type="HostActiveDirectoryAuthentication">ha-ad-auth'
            "</ManagedObjectReference></val>",
        ),
    )


_FAULT = _soap(
    "<soapenv:Fault><faultcode>ServerFaultCode</faultcode>"
    f"<faultstring>The password {html.escape(_PASSWORD)} is too simple</faultstring>"
    '<detail><InvalidArgumentFault xmlns="urn:vim25" xsi:type="InvalidArgument">'
    "<invalidProperty>password</invalidProperty></InvalidArgumentFault></detail>"
    "</soapenv:Fault>"
)


class _EsxiRouter:
    """``POST /sdk``: answer each SOAP call by its method (and read type)."""

    def __init__(self, *, create_fault: bool = False) -> None:
        self.methods: list[str] = []
        self.bodies: dict[str, str] = {}
        self.actions: dict[str, str] = {}
        self.create_fault = create_fault
        self.joined = False

    def __call__(self, request: httpx.Request) -> httpx.Response:
        body = request.content.decode("utf-8")
        method = _local(fromstring(body)[0][0].tag)
        self.methods.append(method)
        self.bodies[method] = body
        self.actions[method] = request.headers.get("SOAPAction", "")
        if method == "RetrieveServiceContent":
            return httpx.Response(200, text=_SERVICE_CONTENT)
        if method == "Login":
            return httpx.Response(
                200,
                text=_response("Login", "<returnval><key>52-abc</key></returnval>"),
                headers={"set-cookie": "vmware_soap_session=AAA; Path=/; HttpOnly"},
            )
        if method == "RetrieveUserGroups":
            return httpx.Response(200, text=_USERS)
        if method == "RetrieveHostAccessControlEntries":
            return httpx.Response(200, text=_ACCESS)
        if method == "CreateUser" and self.create_fault:
            return httpx.Response(500, text=_FAULT)
        if method in ("CreateUser", "RemoveUser"):
            return httpx.Response(200, text=_response(method))
        if method == "JoinDomain_Task":
            return httpx.Response(
                200, text=_response(method, '<returnval type="Task">haTask-7</returnval>')
            )
        if method == "RetrievePropertiesEx":
            return httpx.Response(200, text=self._read(body))
        raise AssertionError(f"unexpected SOAP call {method}")

    def _read(self, body: str) -> str:
        match = re.search(r"<propSet><type>(\w+)</type>", body)
        mo_type = match.group(1) if match else ""
        if mo_type == "HostSystem":
            return _props(
                "HostSystem",
                "ha-host",
                _prop(
                    "configManager.hostAccessManager",
                    '<val type="HostAccessManager">ha-host-access-manager</val>',
                )
                + _prop(
                    "configManager.authenticationManager",
                    '<val type="HostAuthenticationManager">ha-auth-manager</val>',
                ),
            )
        if mo_type == "HostAuthenticationManager":
            return _ad_props(self.joined)
        if mo_type == "Task":
            self.joined = True
            return _props(
                "Task",
                "haTask-7",
                _prop("info", '<val xsi:type="TaskInfo"><state>success</state></val>'),
            )
        raise AssertionError(f"unexpected read of {mo_type!r}")


def _local(tag: Any) -> str:
    return str(tag).rpartition("}")[2]


async def _esxi_call(router: _EsxiRouter, call: Callable[[VmwareRestConnector], Any]) -> Any:
    connector = _connector()
    try:
        async with respx.mock(base_url=_ESXI_BASE) as mock:
            mock.post("/sdk").mock(side_effect=router)
            return await call(connector)
    finally:
        await _close(connector)


async def test_esxi_local_user_list_reads_users_and_roles_over_soap() -> None:
    router = _EsxiRouter()
    out = await _esxi_call(
        router,
        lambda c: typed.host_local_user_list_impl(c, _operator(), _esxi_target(), {}),
    )
    assert out["users"] == [
        {
            "user": "root",
            "full_name": "Administrator",
            "uid": 0,
            "shell_access": True,
            "access_mode": "accessAdmin",
        },
        {
            "user": "svc-old",
            "full_name": None,
            "uid": 1001,
            "shell_access": False,
            "access_mode": None,
        },
    ]
    search = fromstring(router.bodies["RetrieveUserGroups"])[0][0]
    assert [_local(child.tag) for child in search] == [
        "_this",
        "searchStr",
        "exactMatch",
        "findUsers",
        "findGroups",
    ]
    assert router.actions["RetrieveUserGroups"] == f"urn:vim25/{_API_VERSION}"


async def test_esxi_create_user_sends_the_account_spec_over_soap(gate: GateRecorder) -> None:
    router = _EsxiRouter()
    out = await _esxi_call(
        router,
        lambda c: _host_local_users.local_user_create_composite(
            operator=_operator(),
            target=_esxi_target(),
            params={"user_name": "svc-new", "password_secret_ref": _VAULT},
            connector=c,
        ),
    )
    # The read-back still lists the two users of the canned answer.
    assert out["status"] == "not_verified"
    create = fromstring(router.bodies["CreateUser"])[0][0]
    this = next(child for child in create if _local(child.tag) == "_this")
    assert (this.get("type"), this.text) == ("HostLocalAccountManager", "ha-localacctmgr")
    user = next(child for child in create if _local(child.tag) == "user")
    assert [(_local(child.tag), child.text) for child in user] == [
        ("id", "svc-new"),
        ("password", _PASSWORD),
    ]


async def test_esxi_create_user_fault_reaches_the_answer_without_the_password(
    gate: GateRecorder,
) -> None:
    router = _EsxiRouter(create_fault=True)
    out = await _esxi_call(
        router,
        lambda c: _host_local_users.local_user_create_composite(
            operator=_operator(),
            target=_esxi_target(),
            params={"user_name": "svc-new", "password_secret_ref": _VAULT},
            connector=c,
        ),
    )
    assert out["status"] == "failed"
    assert out["error_type"] == "InvalidArgument"
    assert _PASSWORD not in json.dumps(out)


async def test_esxi_delete_user_sends_remove_user_over_soap(gate: GateRecorder) -> None:
    router = _EsxiRouter()
    out = await _esxi_call(
        router,
        lambda c: _host_local_users.local_user_delete_composite(
            operator=_operator(),
            target=_esxi_target(),
            params={"user_name": "svc-old"},
            connector=c,
        ),
    )
    remove = fromstring(router.bodies["RemoveUser"])[0][0]
    assert [(_local(child.tag), child.text) for child in remove][1] == ("userName", "svc-old")
    # The canned answer still lists the user after the call.
    assert out["status"] == "still_present"


async def test_esxi_join_domain_runs_over_soap(gate: GateRecorder) -> None:
    router = _EsxiRouter()
    out = await _esxi_call(
        router,
        lambda c: _host_ad.ad_join_composite(
            operator=_operator(),
            target=_esxi_target(),
            params={
                "domain": "corp.example",
                "user_name": "<join-account>",
                "password_secret_ref": _VAULT,
            },
            connector=c,
        ),
    )
    assert out["status"] == "joined"
    assert out["host"] == "ha-host"
    join = fromstring(router.bodies["JoinDomain_Task"])[0][0]
    assert [_local(child.tag) for child in join] == ["_this", "domainName", "userName", "password"]
    assert _PASSWORD not in json.dumps(out)
