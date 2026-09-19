# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Unit + transport tests for the ``vmware.composite.datastore.refresh`` composite (#3789).

The composite re-probes one datastore's cached capacity/free-space via the vim
``Datastore.RefreshDatastore`` method (and, on request, the deeper
``RefreshDatastoreStorageInfo``), then reads the refreshed ``Datastore.summary``
back through a bounded PropertyCollector read. Two layers are covered:

* **Handler contract** (recording double for ``_post_vmomi_json``): the sub-op
  order, the ``storage_info`` flag, the moid flowing through the method path,
  the summary read-back body shape, the unbox/int-coerce of the summary
  values, and the load-bearing error propagation of a missing datastore.
* **Transport arms** (real httpx via respx): the composite dispatches every
  vim POST through ``VmwareRestConnector._post_vmomi_json``, which serves
  VI-JSON on a vCenter target (``/sdk/vim25/{release}/Datastore/{moid}/...``)
  and hand-rolled SOAP on a standalone ESXi target (the method carried in a
  ``<_this type="Datastore">`` self-reference, the moid a ``server:/export``
  NAS identifier). Both arms are pinned here (#3363 / #2466 seam).
"""

from __future__ import annotations

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
from meho_backplane.connectors.vmware_rest.composites._read import datastore_refresh_composite

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

_REFRESH_PATH = "/Datastore/datastore-42/RefreshDatastore"
_STORAGE_INFO_PATH = "/Datastore/datastore-42/RefreshDatastoreStorageInfo"
_RETRIEVE_PATH = "/PropertyCollector/propertyCollector/RetrievePropertiesEx"


def _make_operator() -> Operator:
    return Operator(
        sub="op-ds-refresh",
        name="Datastore Refresh Test",
        email=None,
        raw_jwt="<test-raw-jwt>",
        tenant_id=UUID("00000000-0000-0000-0000-00000000a0a0"),
        tenant_role=TenantRole.OPERATOR,
    )


def _retrieve_result(moid: str, summary: dict[str, Any]) -> dict[str, Any]:
    """A ``RetrievePropertiesEx`` result carrying one Datastore ``summary`` prop."""
    return {
        "objects": [
            {
                "obj": {"type": "Datastore", "value": moid},
                "propSet": [{"name": "summary", "val": summary}],
            }
        ]
    }


def _local(tag: Any) -> str:
    """Local part of a possibly-namespaced element tag."""
    return str(tag).rpartition("}")[2]


# ---------------------------------------------------------------------------
# Handler contract -- recording double for the vmomi POST seam
# ---------------------------------------------------------------------------


class _RecordingVmomi:
    """Stub connector recording ``_post_vmomi_json`` calls, serving canned JSON.

    The datastore-refresh composite issues only vmomi POST legs (RefreshDatastore
    / RefreshDatastoreStorageInfo / RetrievePropertiesEx), all through
    ``_post_vmomi_json`` with the spec-relative path. Responses are keyed by that
    path; a canned :class:`Exception` value is raised (the transport-failure
    paths).
    """

    def __init__(self, responses: dict[str, Any]) -> None:
        self._responses = responses
        self.calls: list[dict[str, Any]] = []

    async def _post_vmomi_json(
        self,
        target: Any,
        path: str,
        *,
        operator: Operator,
        json: dict[str, Any] | None = None,
    ) -> Any:
        self.calls.append({"path": path, "body": json})
        payload = self._responses[path]
        if isinstance(payload, Exception):
            raise payload
        return payload


_SUMMARY_RAW: dict[str, Any] = {
    "name": "nfs-grown",
    "capacity": 1099511627776,
    "freeSpace": 549755813888,
    "accessible": True,
    "type": "NFS41",
    "url": "ds:///vmfs/volumes/deadbeef/",
}
_SUMMARY_PROJECTED: dict[str, Any] = {
    "name": "nfs-grown",
    "capacity": 1099511627776,
    "free_space": 549755813888,
    "accessible": True,
    "type": "NFS41",
    "url": "ds:///vmfs/volumes/deadbeef/",
}


@pytest.mark.asyncio
async def test_refresh_calls_refresh_datastore_then_reads_summary() -> None:
    """Default: RefreshDatastore fires, then the summary read-back; no StorageInfo."""
    connector = _RecordingVmomi(
        {
            _REFRESH_PATH: {},
            _RETRIEVE_PATH: _retrieve_result("datastore-42", _SUMMARY_RAW),
        }
    )
    result = await datastore_refresh_composite(
        operator=_make_operator(),
        target=object(),
        params={"datastore": "datastore-42"},
        connector=connector,  # type: ignore[arg-type]
    )
    # Order: the refresh method, then the property read-back (StorageInfo skipped).
    assert [c["path"] for c in connector.calls] == [_REFRESH_PATH, _RETRIEVE_PATH]
    assert result == {
        "datastore": "datastore-42",
        "refreshed": True,
        "storage_info_refreshed": False,
        "summary": _SUMMARY_PROJECTED,
    }


@pytest.mark.asyncio
async def test_refresh_storage_info_true_also_calls_storage_info_method() -> None:
    """storage_info=true also fires RefreshDatastoreStorageInfo, before the read-back."""
    connector = _RecordingVmomi(
        {
            _REFRESH_PATH: {},
            _STORAGE_INFO_PATH: {},
            _RETRIEVE_PATH: _retrieve_result("datastore-42", _SUMMARY_RAW),
        }
    )
    result = await datastore_refresh_composite(
        operator=_make_operator(),
        target=object(),
        params={"datastore": "datastore-42", "storage_info": True},
        connector=connector,  # type: ignore[arg-type]
    )
    assert [c["path"] for c in connector.calls] == [
        _REFRESH_PATH,
        _STORAGE_INFO_PATH,
        _RETRIEVE_PATH,
    ]
    assert result["storage_info_refreshed"] is True


@pytest.mark.asyncio
async def test_refresh_read_back_targets_the_datastore_summary() -> None:
    """The read-back is a bounded single-object read of Datastore.summary."""
    connector = _RecordingVmomi(
        {_REFRESH_PATH: {}, _RETRIEVE_PATH: _retrieve_result("datastore-42", _SUMMARY_RAW)}
    )
    await datastore_refresh_composite(
        operator=_make_operator(),
        target=object(),
        params={"datastore": "datastore-42"},
        connector=connector,  # type: ignore[arg-type]
    )
    retrieve_body = connector.calls[-1]["body"]
    spec = retrieve_body["specSet"][0]
    assert spec["propSet"][0]["type"] == "Datastore"
    assert spec["propSet"][0]["pathSet"] == ["summary"]
    assert spec["objectSet"][0]["obj"]["value"] == "datastore-42"
    assert spec["objectSet"][0]["obj"]["type"] == "Datastore"


@pytest.mark.asyncio
async def test_refresh_esxi_nas_moid_flows_into_method_path_and_readback() -> None:
    """A standalone-ESXi ``server:/export`` moid rides both the method path and read-back."""
    moid = "nas.example:/exports/vol"
    refresh_path = f"/Datastore/{moid}/RefreshDatastore"
    connector = _RecordingVmomi(
        {refresh_path: {}, _RETRIEVE_PATH: _retrieve_result(moid, _SUMMARY_RAW)}
    )
    result = await datastore_refresh_composite(
        operator=_make_operator(),
        target=object(),
        params={"datastore": moid},
        connector=connector,  # type: ignore[arg-type]
    )
    assert connector.calls[0]["path"] == refresh_path
    assert connector.calls[-1]["body"]["specSet"][0]["objectSet"][0]["obj"]["value"] == moid
    assert result["datastore"] == moid


@pytest.mark.asyncio
async def test_refresh_summary_unboxes_and_coerces_numeric_strings() -> None:
    """Boxed / string-typed summary values normalise (VI-JSON box + SOAP bare string)."""
    boxed_summary = {
        "name": "nfs-grown",
        # VI-JSON Any-box around the xsd:long (the #3106 shape).
        "capacity": {"_typeName": "long", "_value": "1099511627776"},
        # ESXi SOAP delivers the bare long as a numeric string (soap rule 8).
        "freeSpace": "549755813888",
        "accessible": True,
        "type": "NFS41",
        "url": "ds:///vmfs/volumes/deadbeef/",
    }
    connector = _RecordingVmomi(
        {_REFRESH_PATH: {}, _RETRIEVE_PATH: _retrieve_result("datastore-42", boxed_summary)}
    )
    result = await datastore_refresh_composite(
        operator=_make_operator(),
        target=object(),
        params={"datastore": "datastore-42"},
        connector=connector,  # type: ignore[arg-type]
    )
    assert result["summary"]["capacity"] == 1099511627776
    assert result["summary"]["free_space"] == 549755813888


@pytest.mark.asyncio
async def test_refresh_missing_datastore_propagates_the_transport_fault() -> None:
    """A NotFound (HTTP 500 VimFault) on RefreshDatastore propagates (load-bearing leg).

    The dispatcher's outer branch wraps it as ``connector_error`` for the
    composite parent -- the handler does not swallow it into a status.
    """
    request = httpx.Request("POST", "https://vc.test.invalid" + _REFRESH_PATH)
    not_found = httpx.HTTPStatusError(
        "vim NotFound", request=request, response=httpx.Response(500, request=request)
    )
    connector = _RecordingVmomi({_REFRESH_PATH: not_found})
    with pytest.raises(httpx.HTTPStatusError):
        await datastore_refresh_composite(
            operator=_make_operator(),
            target=object(),
            params={"datastore": "datastore-42"},
            connector=connector,  # type: ignore[arg-type]
        )
    # The read-back never fired: the refresh failed first.
    assert [c["path"] for c in connector.calls] == [_REFRESH_PATH]


# ---------------------------------------------------------------------------
# Transport arms -- real httpx (respx) through _post_vmomi_json
# ---------------------------------------------------------------------------


async def _stub_loader(_target: VsphereTargetLike, _operator: Operator) -> dict[str, str]:
    return {"username": "svc-meho", "password": "stub-password"}


def _make_connector() -> VmwareRestConnector:
    return VmwareRestConnector(session_loader=_stub_loader)


def _patch_no_revoke_aclose(connector: VmwareRestConnector) -> None:
    """Skip the session-revoke leg at teardown (mirrors the vmomi-mount tests)."""

    async def _aclose() -> None:
        connector._session_tokens.clear()
        for client in connector._clients.values():
            await client.aclose()
        connector._clients.clear()

    connector.aclose = _aclose  # type: ignore[method-assign]


@dataclass
class _StubTarget:
    """Structural :class:`VsphereTargetLike`; ``fingerprint`` selects the arm."""

    name: str = "vc-8"
    host: str = "vc-8.test.invalid"
    port: int | None = 443
    secret_ref: str = "vsphere/vc-8"
    auth_model: str | None = AuthModel.SHARED_SERVICE_ACCOUNT.value
    id: UUID = field(default_factory=uuid4)
    tenant_id: UUID = field(default_factory=lambda: UUID(int=0))
    fingerprint: dict[str, Any] | None = None


_VC_BASE = "https://vc-8.test.invalid"


@pytest.mark.asyncio
async def test_refresh_vijson_arm_mounts_on_sdk_vim25_datastore_refresh() -> None:
    """vCenter: the RefreshDatastore POST lands on /sdk/vim25/{release}/Datastore/{moid}/..."""
    connector = _make_connector()
    _patch_no_revoke_aclose(connector)
    refresh_url = "/sdk/vim25/8.0.3.0/Datastore/datastore-42/RefreshDatastore"
    retrieve_url = "/sdk/vim25/8.0.3.0/PropertyCollector/propertyCollector/RetrievePropertiesEx"
    try:
        async with respx.mock(base_url=_VC_BASE) as mock:
            mock.post("/api/session").respond(200, json="tok")
            mock.get("/api/about").respond(200, json={"version": "8.0.3"})
            refresh = mock.post(refresh_url).respond(204)
            mock.post(retrieve_url).respond(
                200, json=_retrieve_result("datastore-42", _SUMMARY_RAW)
            )
            result = await datastore_refresh_composite(
                operator=_make_operator(),
                target=_StubTarget(),
                params={"datastore": "datastore-42"},
                connector=connector,
            )
        assert refresh.called
        assert refresh.call_count == 1
        assert result["refreshed"] is True
        assert result["summary"]["free_space"] == 549755813888
    finally:
        await connector.aclose()


# --- ESXi SOAP arm ---------------------------------------------------------

_ESXI_BASE = "https://esxi-1.test.invalid"
_ESXI_API_VERSION = "8.0.3.0"


def _soap_envelope(inner: str) -> str:
    return (
        '<soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/"'
        ' xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">'
        f"<soapenv:Body>{inner}</soapenv:Body></soapenv:Envelope>"
    )


_ESXI_SERVICE_CONTENT = _soap_envelope(
    '<RetrieveServiceContentResponse xmlns="urn:vim25"><returnval>'
    '<propertyCollector type="PropertyCollector">ha-property-collector</propertyCollector>'
    '<sessionManager type="SessionManager">ha-sessionmgr</sessionManager>'
    f"<about><version>8.0.3</version><apiVersion>{_ESXI_API_VERSION}</apiVersion>"
    "<apiType>HostAgent</apiType></about>"
    "</returnval></RetrieveServiceContentResponse>"
)
_ESXI_LOGIN = _soap_envelope(
    '<LoginResponse xmlns="urn:vim25"><returnval><key>52-abc</key></returnval></LoginResponse>'
)
_ESXI_REFRESH_OK = _soap_envelope(
    '<RefreshDatastoreResponse xmlns="urn:vim25"></RefreshDatastoreResponse>'
)
_ESXI_RETRIEVE = _soap_envelope(
    '<RetrievePropertiesExResponse xmlns="urn:vim25"><returnval>'
    '<objects><obj type="Datastore">nas.example:/exports/vol</obj>'
    '<propSet><name>summary</name><val xsi:type="DatastoreSummary">'
    "<name>nfs-grown</name><capacity>1099511627776</capacity>"
    "<freeSpace>549755813888</freeSpace><accessible>true</accessible>"
    "<type>NFS41</type><url>ds:///vmfs/volumes/deadbeef/</url>"
    "</val></propSet></objects>"
    "</returnval></RetrievePropertiesExResponse>"
)


def _esxi_soap_method(body: str) -> str:
    for method in (
        "RetrieveServiceContent",
        "Login",
        "Logout",
        "RefreshDatastoreStorageInfo",
        "RefreshDatastore",
        "RetrievePropertiesEx",
    ):
        if f"<{method} " in body or f"<{method}>" in body:
            return method
    return "?"


class _EsxiSdkRouter:
    """respx ``POST /sdk`` side-effect dispatching SOAP posts by method."""

    def __init__(self) -> None:
        self.methods: list[str] = []
        self.bodies: list[str] = []
        self.soap_actions: list[str] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        body = request.content.decode("utf-8")
        method = _esxi_soap_method(body)
        self.methods.append(method)
        self.bodies.append(body)
        self.soap_actions.append(request.headers.get("SOAPAction", ""))
        if method == "RetrieveServiceContent":
            return httpx.Response(200, text=_ESXI_SERVICE_CONTENT)
        if method == "Login":
            return httpx.Response(
                200,
                text=_ESXI_LOGIN,
                headers={"set-cookie": "vmware_soap_session=AAA; Path=/; HttpOnly"},
            )
        if method in ("RefreshDatastore", "RefreshDatastoreStorageInfo"):
            return httpx.Response(200, text=_ESXI_REFRESH_OK)
        if method == "RetrievePropertiesEx":
            return httpx.Response(200, text=_ESXI_RETRIEVE)
        return httpx.Response(200, text=_soap_envelope("<ok/>"))


@pytest.mark.asyncio
async def test_refresh_esxi_soap_arm_this_type_datastore_and_action() -> None:
    """Standalone ESXi: RefreshDatastore rides SOAP with ``_this type=Datastore`` (raw moid)."""
    connector = _make_connector()
    _patch_no_revoke_aclose(connector)
    router = _EsxiSdkRouter()
    moid = "nas.example:/exports/vol"
    target = _StubTarget(
        name="esxi-1",
        host="esxi-1.test.invalid",
        secret_ref="vsphere/esxi-1",
        fingerprint={"product": "esxi", "reachable": True, "version": "8.0.3"},
    )
    try:
        async with respx.mock(base_url=_ESXI_BASE) as mock:
            mock.post("/sdk").mock(side_effect=router)
            result = await datastore_refresh_composite(
                operator=_make_operator(),
                target=target,
                params={"datastore": moid},
                connector=connector,
            )
    finally:
        await connector.aclose()

    # Establish (ServiceContent + Login) then the refresh + summary read, all SOAP.
    assert router.methods == [
        "RetrieveServiceContent",
        "Login",
        "RefreshDatastore",
        "RetrievePropertiesEx",
    ]
    refresh_body = router.bodies[router.methods.index("RefreshDatastore")]
    root = fromstring(refresh_body)
    method_el = root[0][0]
    assert _local(method_el.tag) == "RefreshDatastore"
    this = next(c for c in method_el if _local(c.tag) == "_this")
    assert this.get("type") == "Datastore"
    assert this.text == moid  # the server:/export moid rides _this raw (XML-safe)
    # Every op past the bootstrap is pinned to the host's apiVersion.
    refresh_action = router.soap_actions[router.methods.index("RefreshDatastore")]
    assert refresh_action == f"urn:vim25/{_ESXI_API_VERSION}"
    assert result == {
        "datastore": moid,
        "refreshed": True,
        "storage_info_refreshed": False,
        "summary": {
            "name": "nfs-grown",
            "capacity": 1099511627776,
            "free_space": 549755813888,
            "accessible": True,
            "type": "NFS41",
            "url": "ds:///vmfs/volumes/deadbeef/",
        },
    }
