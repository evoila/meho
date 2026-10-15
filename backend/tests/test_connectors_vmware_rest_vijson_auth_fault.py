# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""VI-JSON HTTP 500 auth faults on the vCenter path (#3566).

vCenter reports a vim method fault on the VI-JSON surface as HTTP 500 with
the fault object as the JSON body. When the fault is ``NotAuthenticated``
(a host-relayed call such as a guest operation, whose host-gateway token
went stale), :meth:`VmwareRestConnector._post_vmomi_json` drops the cached
session, logs in again, and retries that one call. ``InvalidLogin`` /
``NoPermission`` raise :class:`ConnectorAuthError` at once. Other faults and
other statuses (including 401, which the dispatcher recovers) are unchanged.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
import respx

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors._shared.vcf_auth import ConnectorAuthError
from meho_backplane.connectors.schemas import AuthModel
from meho_backplane.connectors.vmware_rest import VmwareRestConnector, VsphereTargetLike
from meho_backplane.connectors.vmware_rest.composites import _guest
from meho_backplane.connectors.vmware_rest.connector import _vijson_auth_fault_type

_BASE = "https://vc-8.test.invalid"
_METHOD_PATH = "/GuestProcessManager/pm-1/ListProcessesInGuest"
_VIJSON_URL = f"/sdk/vim25/8.0.3.0{_METHOD_PATH}"
_RETRIEVE_URL = "/sdk/vim25/8.0.3.0/PropertyCollector/propertyCollector/RetrievePropertiesEx"
_SESSION_HEADER = "vmware-api-session-id"


def _fault(type_name: str) -> dict[str, Any]:
    return {
        "_typeName": type_name,
        "object": {"type": "GuestProcessManager", "value": "pm-1"},
        "privilegeId": "",
        "faultstring": "The session is not authenticated.",
    }


@dataclass
class _StubTarget:
    name: str = "vc-8"
    host: str = "vc-8.test.invalid"
    port: int | None = 443
    secret_ref: str = "vsphere/vc-8"
    auth_model: str | None = AuthModel.SHARED_SERVICE_ACCOUNT.value
    id: UUID = field(default_factory=uuid4)
    tenant_id: UUID = field(default_factory=lambda: UUID(int=0))


def _operator() -> Operator:
    return Operator(
        sub="op-vijson-auth",
        name="VI-JSON Auth Test",
        email=None,
        raw_jwt="<test-raw-jwt>",
        tenant_id=UUID("00000000-0000-0000-0000-00000000a0a0"),
        tenant_role=TenantRole.OPERATOR,
    )


async def _stub_loader(_target: VsphereTargetLike, _operator: Operator) -> dict[str, str]:
    return {"username": "svc-meho", "password": "stub-password"}


def _make_connector() -> VmwareRestConnector:
    connector = VmwareRestConnector(session_loader=_stub_loader)

    async def _aclose() -> None:
        connector._session_tokens.clear()
        for client in connector._clients.values():
            await client.aclose()
        connector._clients.clear()

    connector.aclose = _aclose  # type: ignore[method-assign]
    return connector


def _mock_session(mock: respx.MockRouter) -> respx.Route:
    """Two distinct session tokens, so a test can see which call used which."""
    mock.get("/api/about").respond(200, json={"version": "8.0.3"})
    return mock.post("/api/session").mock(
        side_effect=[httpx.Response(200, json="tok-1"), httpx.Response(200, json="tok-2")]
    )


async def _call(connector: VmwareRestConnector) -> dict[str, Any]:
    return await connector._post_vmomi_json(
        _StubTarget(), _METHOD_PATH, operator=_operator(), json={"vm": {}}
    )


# ---------------------------------------------------------------------------
# Classifier truth table
# ---------------------------------------------------------------------------


def _status_error(status: int, content: bytes) -> httpx.HTTPStatusError:
    request = httpx.Request("POST", f"{_BASE}{_VIJSON_URL}")
    response = httpx.Response(status, content=content, request=request)
    return httpx.HTTPStatusError("err", request=request, response=response)


@pytest.mark.parametrize(
    ("status", "content", "expected"),
    [
        (500, b'{"_typeName":"NotAuthenticated"}', "NotAuthenticated"),
        (500, b'{"_typeName":"InvalidLogin"}', "InvalidLogin"),
        (500, b'{"_typeName":"NoPermission"}', "NoPermission"),
        (500, b'{"_typeName":"InvalidArgument"}', None),
        (500, b'{"_typeName":"PlatformConfigFault"}', None),
        (500, b"not json", None),
        (500, b"", None),
        # Only a 500 fault body counts; a 401 stays on the dispatcher's path.
        (401, b'{"_typeName":"NotAuthenticated"}', None),
        (503, b'{"_typeName":"NotAuthenticated"}', None),
    ],
)
def test_vijson_auth_fault_type_truth_table(
    status: int, content: bytes, expected: str | None
) -> None:
    assert _vijson_auth_fault_type(_status_error(status, content)) == expected


# ---------------------------------------------------------------------------
# _post_vmomi_json against a real httpx (respx) client
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_not_authenticated_relogs_in_and_retries_once() -> None:
    """500 NotAuthenticated -> drop the session, log in again, retry -> success."""
    connector = _make_connector()
    try:
        async with respx.mock(base_url=_BASE) as mock:
            session = _mock_session(mock)
            method = mock.post(_VIJSON_URL).mock(
                side_effect=[
                    httpx.Response(500, json=_fault("NotAuthenticated")),
                    httpx.Response(200, json=[{"name": "sshd", "pid": 812}]),
                ]
            )
            result = await _call(connector)
        assert result == [{"name": "sshd", "pid": 812}]
        assert session.call_count == 2
        assert method.call_count == 2
        assert method.calls[0].request.headers[_SESSION_HEADER] == "tok-1"
        assert method.calls[1].request.headers[_SESSION_HEADER] == "tok-2"
    finally:
        await connector.aclose()


@pytest.mark.asyncio
async def test_not_authenticated_twice_raises_after_relogin_no_loop() -> None:
    """A second NotAuthenticated on the fresh session raises; no third attempt."""
    connector = _make_connector()
    try:
        async with respx.mock(base_url=_BASE) as mock:
            session = _mock_session(mock)
            method = mock.post(_VIJSON_URL).respond(500, json=_fault("NotAuthenticated"))
            with pytest.raises(ConnectorAuthError) as excinfo:
                await _call(connector)
        assert session.call_count == 2
        assert method.call_count == 2
        err = excinfo.value
        assert err.stage == "after_relogin"
        assert err.status_code == 500
        assert err.cause == "vim_fault_NotAuthenticated_after_relogin"
        assert err.target_name == "vc-8"
        assert err.remediation is not None
        assert "do NOT restage" in err.remediation
        assert isinstance(err.__cause__, httpx.HTTPStatusError)
    finally:
        await connector.aclose()


@pytest.mark.parametrize("fault_type", ["NoPermission", "InvalidLogin"])
@pytest.mark.asyncio
async def test_no_permission_and_invalid_login_raise_without_retry(fault_type: str) -> None:
    """A fresh session cannot fix these, so they raise on the first try."""
    connector = _make_connector()
    try:
        async with respx.mock(base_url=_BASE) as mock:
            session = _mock_session(mock)
            method = mock.post(_VIJSON_URL).respond(500, json=_fault(fault_type))
            with pytest.raises(ConnectorAuthError) as excinfo:
                await _call(connector)
        assert session.call_count == 1
        assert method.call_count == 1
        err = excinfo.value
        assert err.stage == "call"
        assert err.status_code == 500
        assert err.cause == f"vim_fault_{fault_type}"
    finally:
        await connector.aclose()


@pytest.mark.parametrize("fault_type", ["InvalidArgument", "PlatformConfigFault"])
@pytest.mark.asyncio
async def test_non_auth_500_fault_is_unchanged(fault_type: str) -> None:
    """Any other vim fault keeps the plain HTTPStatusError (-> connector_error)."""
    connector = _make_connector()
    try:
        async with respx.mock(base_url=_BASE) as mock:
            session = _mock_session(mock)
            method = mock.post(_VIJSON_URL).respond(500, json=_fault(fault_type))
            with pytest.raises(httpx.HTTPStatusError) as excinfo:
                await _call(connector)
        assert not isinstance(excinfo.value, ConnectorAuthError)
        assert excinfo.value.response.status_code == 500
        assert session.call_count == 1
        assert method.call_count == 1
    finally:
        await connector.aclose()


@pytest.mark.asyncio
async def test_401_is_left_to_the_dispatcher() -> None:
    """A 401 is not retried here; it propagates for the dispatcher's #2067 recovery."""
    connector = _make_connector()
    try:
        async with respx.mock(base_url=_BASE) as mock:
            session = _mock_session(mock)
            method = mock.post(_VIJSON_URL).respond(401, json={})
            with pytest.raises(httpx.HTTPStatusError) as excinfo:
                await _call(connector)
        assert excinfo.value.response.status_code == 401
        assert session.call_count == 1
        assert method.call_count == 1
    finally:
        await connector.aclose()


# ---------------------------------------------------------------------------
# End to end: a guest operation recovers within one call
# ---------------------------------------------------------------------------


async def _guest_creds(
    target: Any, operator: Operator, *, fields: tuple[str, ...]
) -> dict[str, str]:
    return {"guest_username": "svc-guest", "guest_password": "guest-pw"}


@pytest.mark.asyncio
async def test_guest_process_list_recovers_from_not_authenticated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The observed failure: ListProcessesInGuest 500s NotAuthenticated, then works.

    Only the rejected call is retried. The earlier manager lookup
    (RetrievePropertiesEx) runs once, so steps before the failing call of a
    multi-step operation never run twice.
    """
    monkeypatch.setattr(_guest, "load_basic_credentials", _guest_creds)
    manager_props = {
        "objects": [
            {
                "obj": {"type": "GuestOperationsManager", "value": "guestOperationsManager"},
                "propSet": [
                    {
                        "name": "processManager",
                        "val": {
                            "_typeName": "ManagedObjectReference",
                            "type": "GuestProcessManager",
                            "value": "pm-1",
                        },
                    }
                ],
            }
        ]
    }
    procs = [{"name": "sshd", "pid": 812, "owner": "root"}]
    connector = _make_connector()
    try:
        async with respx.mock(base_url=_BASE) as mock:
            session = _mock_session(mock)
            retrieve = mock.post(_RETRIEVE_URL).respond(200, json=manager_props)
            method = mock.post(_VIJSON_URL).mock(
                side_effect=[
                    httpx.Response(500, json=_fault("NotAuthenticated")),
                    httpx.Response(200, json=procs),
                ]
            )
            out = await _guest.guest_process_list_composite(
                operator=_operator(),
                target=_StubTarget(),
                params={"vm": "vm-1001"},
                connector=connector,
            )
        assert out["processes"] == procs
        assert out["count"] == 1
        assert session.call_count == 2
        assert retrieve.call_count == 1
        assert method.call_count == 2
    finally:
        await connector.aclose()
