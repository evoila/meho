# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Unit tests for the standalone-ESXi SOAP session branch (#3363).

#3332 shipped standalone-ESXi host resolution (``ha-host``), the vmomi
seam, and ``product=esxi`` fingerprint stamping, but left session
establishment on the two vSphere-Automation vAPI endpoints
(``POST /api/session`` + the ``/rest/com/vmware/cis/session`` 404-fallback)
that exist on vCenter only. #3363's first cut assumed a standalone host
would serve the VI-JSON surface (``/sdk/vim25/{release}/…``); that premise
is **disproven live** — that surface is vCenter-only and every VI-JSON POST
there 500s with a SOAP expat fault, while ESXi's ``/api/*`` is a JSON-RPC
2.0 handler (``POST /api/session`` → HTTP 400 "Unsupported content type").
The correct transport is **hand-rolled SOAP 1.1 over ``POST /sdk``**:
``RetrieveServiceContent`` (unauthenticated) → ``SessionManager.Login`` (sets
a ``vmware_soap_session`` cookie) → the host reads/writes as SOAP methods.

Coverage matrix (per #3363 acceptance criteria):

* **Branch select — first probe.** The JSON-RPC-400 (or "Unsupported
  content type") signature on ``POST /api/session`` selects the SOAP branch
  before any fingerprint exists.
* **Branch select — fingerprinted.** A ``product=esxi`` target goes straight
  to the SOAP branch — no ``POST /api/session`` at all.
* **Ordered establish.** ``RetrieveServiceContent`` before
  ``SessionManager.Login``; Login's ``_this`` is the ServiceContent-provided
  SessionManager moid.
* **Cookie, not header.** ``auth_headers()`` adds no header for ESXi; the
  ``vmware_soap_session`` cookie carries auth on the pooled client.
* **Credential hidden.** An ``InvalidLogin`` fault → ``ConnectorAuthError``
  whose message never contains the username / password.
* **moid remap.** ``RetrievePropertiesEx`` on the ``propertyCollector``
  literal is remapped to the HostAgent's ServiceContent PC moid.
* **fingerprint.** A standalone ESXi target → ``reachable=True``,
  ``product=esxi``, ``version=about.version`` via SOAP RetrieveServiceContent.
* **Teardown / Logout.** SOAP ``SessionManager.Logout`` on ``invalidate_session``
  and ``aclose`` (never ``DELETE /api/session``).
* **Cold re-login.** ``invalidate_session`` → the next ``auth_headers`` re-runs
  the SOAP establish.
* **vCenter regression.** A genuine (non-JSON-RPC) 400 never enters the SOAP
  branch; a real vCenter still mints via ``POST /api/session``.
* **Flight-recorder span.** The vendor-call span never serialises the ``/sdk``
  SOAP envelope (the Login ``<password>`` cannot leak into a captured span).

The SOAP wire envelopes are the **captured, scrubbed** responses from the
#3363 State-2 run against a standalone ESXi 9.1 host
(``tests/fixtures/vmware_esxi_soap/``) -- so the session tests exercise the
establish → read → write → teardown wire against real hardware bytes, not
WSDL models. The ``/api/session`` 400 body is the exact live JSON-RPC
answer. The ``set-cookie`` header (the auth) is synthesised: it is a
transport header, not part of the captured response body.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
import respx

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors._shared.cache_key import target_cache_key
from meho_backplane.connectors._shared.vcf_auth import ConnectorAuthError
from meho_backplane.connectors.schemas import AuthModel
from meho_backplane.connectors.vmware_rest import VmwareRestConnector, VsphereTargetLike
from meho_backplane.connectors.vmware_rest import connector as connector_module
from meho_backplane.connectors.vmware_rest.typed_ops_host_storage_devices import (
    _extract_host_props,
    _map_scsi_lun,
    build_host_storage_devices_retrieve_params,
)
from meho_backplane.settings import get_settings

_ESXI_HOST = "esxi-standalone.test.invalid"
_ESXI_BASE = f"https://{_ESXI_HOST}"
_SDK = "/sdk"

#: ServiceContent-provided moids (NOT the vCenter ``propertyCollector`` /
#: ``ha-sessionmgr`` literals a naive branch would hard-code).
_PC_MOID = "ha-property-collector"
_SM_MOID = "ha-sessionmgr"
_ABOUT_VERSION = "9.1.0"
_SOAP_COOKIE = "vmware_soap_session"
_COOKIE_VALUE = "52a1b2c3-cookie-value"

_SOAP_ENV = "http://schemas.xmlsoap.org/soap/envelope/"
_XSI = "http://www.w3.org/2001/XMLSchema-instance"

_LIVE_FIXTURES = Path(__file__).parent / "fixtures" / "vmware_esxi_soap"


def _live(name: str) -> str:
    """Read a captured+scrubbed #3363 State-2 envelope fixture (real hardware bytes)."""
    return (_LIVE_FIXTURES / name).read_text()


def _envelope(inner: str) -> str:
    return (
        f'<soapenv:Envelope xmlns:soapenv="{_SOAP_ENV}" xmlns:xsi="{_XSI}">'
        f"<soapenv:Body>{inner}</soapenv:Body></soapenv:Envelope>"
    )


# The establish / read / write / teardown wire, replayed from the captured
# live envelopes. ``service_content.xml`` carries the same ``_PC_MOID`` /
# ``_SM_MOID`` / ``_ABOUT_VERSION`` the constants above name (they are the
# vendor-universal HostAgent singletons) plus the real ``about.apiVersion``.
_SERVICE_CONTENT_XML = _live("service_content.xml")
_LOGIN_OK_XML = _live("login.xml")
_LOGOUT_OK_XML = _live("logout.xml")

#: The live scsiLun read captured **after** ``MarkAsSsd_Task`` -- one
#: HostScsiDisk whose bare ``<ssd>true</ssd>`` proves the #3332 typing trap on
#: real bytes, fed through the unchanged consumer extractors for parity.
_SCSI_LUN_RETRIEVE_XML = _live("retrieve_scsi_luns_marked_ssd.xml")
#: The T3 disk's real capacity in that envelope (512-byte blocks x block count).
_SCSI_LUN_CAPACITY_BYTES = 322122547200

#: Synchronous MoRef returnvals for the two host write methods (live).
_CREATE_NAS_OK_XML = _live("create_nas_datastore.xml")
_DATASTORE_MOID = "nas01.example.invalid:/exports/share"  # the scrubbed live Datastore moid
_MARK_SSD_TASK_OK_XML = _live("mark_as_ssd_task.xml")

# The two fault envelopes stay hand-written vim25 shapes: the State-2 run
# authenticated and every write succeeded, so no InvalidLogin / HostConfigFault
# was captured. They model the vendor fault shape the connector maps.
_INVALID_LOGIN_FAULT_XML = _envelope(
    "<soapenv:Fault><faultcode>ServerFaultCode</faultcode>"
    "<faultstring>Cannot complete login due to an incorrect user name or password."
    "</faultstring>"
    '<detail><InvalidLoginFault xsi:type="InvalidLogin"></InvalidLoginFault></detail>'
    "</soapenv:Fault>"
)

#: A write-fault (HostConfigFault) HTTP 500 for the non-auth fault path.
_HOST_CONFIG_FAULT_XML = _envelope(
    "<soapenv:Fault><faultcode>ServerFaultCode</faultcode>"
    "<faultstring>The NFS export is unreachable.</faultstring>"
    '<detail><HostConfigFaultFault xsi:type="HostConfigFault"></HostConfigFaultFault></detail>'
    "</soapenv:Fault>"
)

#: ESXi's JSON-RPC 2.0 answer to a bodyless POST /api/session (Basic auth) --
#: the **exact** live body captured in the #3363 State-2 run (HTTP 400,
#: content-type ``text/plain``, ``id: null``, "Unsupported content type: "),
#: loaded from the committed fixture so it stays the single source of truth.
_JSONRPC_400_BODY: dict[str, Any] = json.loads(_live("api_session_400.json"))


def _soap_method(body: str) -> str:
    """Return the vim method name in a SOAP request envelope body."""
    for method in (
        "RetrieveServiceContent",
        "RetrievePropertiesEx",
        "Login",
        "Logout",
        "QueryBootDevices",
        "CreateNasDatastore",
        "MarkAsSsd_Task",
        "MarkAsNonSsd_Task",
    ):
        if f"<{method} " in body or f"<{method}>" in body:
            return method
    return "?"


class _SdkRouter:
    """A respx ``/sdk`` side-effect that dispatches SOAP posts by method.

    All SOAP posts hit the one ``POST /sdk`` endpoint, so the route branches
    on the method element in the request envelope. Records the ordered method
    names, the raw request bodies, and the ``SOAPAction`` header per call for
    assertions.
    """

    def __init__(self, *, login_fault: bool = False, retrieve_xml: str | None = None) -> None:
        self.methods: list[str] = []
        self.bodies: list[str] = []
        self.soap_actions: list[str] = []
        self._login_fault = login_fault
        self._retrieve_xml = retrieve_xml

    def __call__(self, request: httpx.Request) -> httpx.Response:
        body = request.content.decode("utf-8")
        method = _soap_method(body)
        self.methods.append(method)
        self.bodies.append(body)
        self.soap_actions.append(request.headers.get("SOAPAction", ""))
        if method == "RetrieveServiceContent":
            return httpx.Response(200, text=_SERVICE_CONTENT_XML)
        if method == "Login":
            if self._login_fault:
                return httpx.Response(500, text=_INVALID_LOGIN_FAULT_XML)
            return httpx.Response(
                200,
                text=_LOGIN_OK_XML,
                headers={"set-cookie": f"{_SOAP_COOKIE}={_COOKIE_VALUE}; Path=/; HttpOnly"},
            )
        if method == "Logout":
            return httpx.Response(200, text=_LOGOUT_OK_XML)
        if method == "RetrievePropertiesEx":
            return httpx.Response(200, text=self._retrieve_xml or _SCSI_LUN_RETRIEVE_XML)
        if method == "CreateNasDatastore":
            return httpx.Response(200, text=_CREATE_NAS_OK_XML)
        if method == "MarkAsSsd_Task":
            return httpx.Response(200, text=_MARK_SSD_TASK_OK_XML)
        return httpx.Response(500, text="<unexpected/>")


def _make_operator(raw_jwt: str = "op.test.jwt") -> Operator:
    return Operator(
        sub="test-operator",
        name=None,
        email=None,
        raw_jwt=raw_jwt,
        tenant_id=UUID(int=0),
        tenant_role=TenantRole.OPERATOR,
    )


@pytest.fixture(autouse=True)
def _settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Pin the chassis env vars ``Settings`` reads at construction time."""
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


@dataclass
class _StubTarget:
    """Satisfies ``VsphereTargetLike`` structurally; carries an optional fingerprint.

    ``fingerprint=None`` (unprobed) classifies as vCenter — the first-probe
    case; ``{"product": "esxi", "reachable": True, ...}`` classifies as a
    standalone ESXi target (``classify_host_target``, #3332).
    """

    name: str = "esxi-standalone"
    host: str = _ESXI_HOST
    port: int | None = 443
    secret_ref: str = "vsphere/esxi-standalone"
    auth_model: str | None = AuthModel.SHARED_SERVICE_ACCOUNT.value
    tls_server_name: str | None = None
    fingerprint: dict[str, Any] | None = None
    id: UUID = field(default_factory=uuid4)
    tenant_id: UUID = field(default_factory=lambda: UUID(int=0))


def _esxi_fingerprinted() -> _StubTarget:
    return _StubTarget(fingerprint={"product": "esxi", "reachable": True, "version": "9.1"})


async def _stub_loader(_target: VsphereTargetLike, _operator: Operator) -> dict[str, str]:
    return {"username": "svc-meho", "password": "stub-password"}


def _make_connector() -> VmwareRestConnector:
    return VmwareRestConnector(session_loader=_stub_loader)


def _patch_no_revoke_aclose(connector: VmwareRestConnector) -> None:
    """Replace ``aclose`` with a revoke-free pool tear-down.

    Tests that don't exercise the revoke leg would otherwise trip respx's
    assert-all-mocked on the shutdown Logout/DELETE. Clears the #3363 SOAP
    caches alongside the pre-existing ones.
    """

    async def _aclose() -> None:
        connector._session_tokens.clear()
        connector._session_paths.clear()
        connector._session_flavors.clear()
        connector._session_extensions.clear()
        connector._about_versions.clear()
        connector._esxi_pc_moids.clear()
        connector._esxi_session_manager_moids.clear()
        connector._esxi_api_versions.clear()
        for client in connector._clients.values():
            await client.aclose()
        connector._clients.clear()

    connector.aclose = _aclose  # type: ignore[method-assign]


# ---------------------------------------------------------------------------
# Branch select
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_first_probe_jsonrpc_400_selects_soap_branch() -> None:
    """A bodyless POST /api/session 400 (JSON-RPC) selects the SOAP establish."""
    connector = _make_connector()
    _patch_no_revoke_aclose(connector)
    router = _SdkRouter()

    async with respx.mock(base_url=_ESXI_BASE) as mock:
        session_route = mock.post("/api/session").respond(400, json=_JSONRPC_400_BODY)
        mock.post(_SDK).mock(side_effect=router)
        headers = await connector.auth_headers(_StubTarget(), _make_operator())

    # ESXi auth is the cookie, not a header — auth_headers adds nothing.
    assert headers == {}
    # The vAPI probe fired once (its 400 selected the branch); establish then
    # ran ServiceContent + Login over SOAP.
    assert session_route.call_count == 1
    assert router.methods == ["RetrieveServiceContent", "Login"]


@pytest.mark.asyncio
async def test_first_probe_unsupported_content_type_text_selects_soap_branch() -> None:
    """A 400 whose body only carries the "Unsupported content type" text still selects SOAP."""
    connector = _make_connector()
    _patch_no_revoke_aclose(connector)
    router = _SdkRouter()

    async with respx.mock(base_url=_ESXI_BASE) as mock:
        mock.post("/api/session").respond(400, text="Unsupported content type: ")
        mock.post(_SDK).mock(side_effect=router)
        headers = await connector.auth_headers(_StubTarget(), _make_operator())

    assert headers == {}
    assert router.methods == ["RetrieveServiceContent", "Login"]


@pytest.mark.asyncio
async def test_fingerprinted_esxi_skips_api_session_entirely() -> None:
    """A target already fingerprinted product=esxi goes straight to the SOAP establish."""
    connector = _make_connector()
    _patch_no_revoke_aclose(connector)
    router = _SdkRouter()

    async with respx.mock(base_url=_ESXI_BASE, assert_all_called=False) as mock:
        session_route = mock.post("/api/session").respond(400, json=_JSONRPC_400_BODY)
        mock.post(_SDK).mock(side_effect=router)
        headers = await connector.auth_headers(_esxi_fingerprinted(), _make_operator())

    assert headers == {}
    assert router.methods == ["RetrieveServiceContent", "Login"]
    # The fingerprint already said ESXi, so the vAPI session path is never hit.
    assert session_route.call_count == 0


# ---------------------------------------------------------------------------
# Ordered establish + cookie auth
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_establish_orders_service_content_then_login_on_provided_moid() -> None:
    """ServiceContent precedes Login; Login's _this is the ServiceContent SessionManager moid."""
    connector = _make_connector()
    _patch_no_revoke_aclose(connector)
    router = _SdkRouter()

    async with respx.mock(base_url=_ESXI_BASE) as mock:
        mock.post(_SDK).mock(side_effect=router)
        await connector.auth_headers(_esxi_fingerprinted(), _make_operator())

    assert router.methods == ["RetrieveServiceContent", "Login"]
    login_body = router.bodies[1]
    # Login is invoked on the SessionManager moid RetrieveServiceContent returned.
    assert f'<_this type="SessionManager">{_SM_MOID}</_this>' in login_body
    # The credential rides the Login body (never HTTP Basic).
    assert "<userName>svc-meho</userName>" in login_body
    assert "<password>stub-password</password>" in login_body


@pytest.mark.asyncio
async def test_auth_is_cookie_not_header_on_host_reads() -> None:
    """A host read carries the vmware_soap_session cookie and no vmware-api-session-id header."""
    connector = _make_connector()
    _patch_no_revoke_aclose(connector)
    router = _SdkRouter()
    target = _esxi_fingerprinted()

    async with respx.mock(base_url=_ESXI_BASE) as mock:
        route = mock.post(_SDK).mock(side_effect=router)
        await connector._post_vmomi_json(
            target,
            "/PropertyCollector/propertyCollector/RetrievePropertiesEx",
            operator=_make_operator(),
            json=build_host_storage_devices_retrieve_params("ha-host"),
        )

    # The last /sdk request is the RetrievePropertiesEx read (after establish).
    read_request = route.calls[-1].request
    assert f"{_SOAP_COOKIE}={_COOKIE_VALUE}" in read_request.headers.get("cookie", "")
    assert read_request.headers.get("vmware-api-session-id") is None


@pytest.mark.asyncio
async def test_session_reused_across_auth_calls() -> None:
    """Two auth_headers calls against one ESXi target → exactly one establish."""
    connector = _make_connector()
    _patch_no_revoke_aclose(connector)
    router = _SdkRouter()
    target = _esxi_fingerprinted()

    async with respx.mock(base_url=_ESXI_BASE) as mock:
        mock.post(_SDK).mock(side_effect=router)
        h1 = await connector.auth_headers(target, _make_operator())
        h2 = await connector.auth_headers(target, _make_operator())

    assert h1 == h2 == {}
    assert router.methods == ["RetrieveServiceContent", "Login"]


# ---------------------------------------------------------------------------
# moid remap
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_retrieve_properties_ex_remaps_property_collector_moid() -> None:
    """The vCenter propertyCollector literal is remapped to the HostAgent's PC moid."""
    connector = _make_connector()
    _patch_no_revoke_aclose(connector)
    router = _SdkRouter()
    target = _esxi_fingerprinted()

    async with respx.mock(base_url=_ESXI_BASE) as mock:
        mock.post(_SDK).mock(side_effect=router)
        await connector._post_vmomi_json(
            target,
            "/PropertyCollector/propertyCollector/RetrievePropertiesEx",
            operator=_make_operator(),
            json=build_host_storage_devices_retrieve_params("ha-host"),
        )

    read_body = router.bodies[-1]
    # The ServiceContent PC moid is substituted for the vCenter literal.
    assert f'<_this type="PropertyCollector">{_PC_MOID}</_this>' in read_body
    assert '<_this type="PropertyCollector">propertyCollector</_this>' not in read_body


@pytest.mark.asyncio
async def test_ops_carry_versioned_soap_action_bootstrap_does_not() -> None:
    """Every op past the bootstrap pins ``SOAPAction: urn:vim25/<about.apiVersion>``.

    Live regression (#3363 State-2): an empty ``SOAPAction`` resolves the
    method against the host baseline (2.5u2) schema, which lacks
    ``RetrievePropertiesEx`` -- the host 500s ``InvalidRequest: Unable to
    resolve WSDL method name``. ``RetrieveServiceContent`` + ``Login`` ride the
    baseline (empty action); the connector reads ``about.apiVersion`` from
    ServiceContent and pins it on every subsequent ``/sdk`` POST."""
    connector = _make_connector()
    _patch_no_revoke_aclose(connector)
    router = _SdkRouter()
    target = _esxi_fingerprinted()

    async with respx.mock(base_url=_ESXI_BASE) as mock:
        mock.post(_SDK).mock(side_effect=router)
        await connector._post_vmomi_json(
            target,
            "/PropertyCollector/propertyCollector/RetrievePropertiesEx",
            operator=_make_operator(),
            json=build_host_storage_devices_retrieve_params("ha-host"),
        )

    actions = dict(zip(router.methods, router.soap_actions, strict=True))
    # Bootstrap pair rides the baseline schema -> no version announced.
    assert actions["RetrieveServiceContent"] == ""
    assert actions["Login"] == ""
    # The op announces the host's own apiVersion (9.1.0.0 in service_content.xml).
    assert actions["RetrievePropertiesEx"] == "urn:vim25/9.1.0.0"


@pytest.mark.asyncio
async def test_host_read_deserialises_to_vijson_shape_through_consumers() -> None:
    """A RetrievePropertiesEx read parses back into the exact shape the consumers read."""
    connector = _make_connector()
    _patch_no_revoke_aclose(connector)
    router = _SdkRouter()
    target = _esxi_fingerprinted()

    async with respx.mock(base_url=_ESXI_BASE) as mock:
        mock.post(_SDK).mock(side_effect=router)
        result = await connector._post_vmomi_json(
            target,
            "/PropertyCollector/propertyCollector/RetrievePropertiesEx",
            operator=_make_operator(),
            json=build_host_storage_devices_retrieve_params("ha-host"),
        )

    # Same {"objects": [...]} shape the VI-JSON path returns — fed through the
    # unchanged consumer extractors proves parity (the #3332 corruption class
    # does not recur: bare <ssd>true</ssd> survives to ssd is True).
    props = _extract_host_props(result)
    luns = props["config.storageDevice.scsiLun"]
    assert isinstance(luns, list)
    disk = _map_scsi_lun(luns[0], None)
    assert disk["ssd"] is True
    assert disk["local"] is True
    assert disk["capacity_bytes"] == _SCSI_LUN_CAPACITY_BYTES


# ---------------------------------------------------------------------------
# Write-method routing (datastore_mount_nfs / disk_mark_flash callers)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_nas_datastore_routes_through_soap_and_returns_moref() -> None:
    """The datastore_mount_nfs caller's CreateNasDatastore rides SOAP → a Datastore MoRef."""
    connector = _make_connector()
    _patch_no_revoke_aclose(connector)
    router = _SdkRouter()
    target = _esxi_fingerprinted()

    async with respx.mock(base_url=_ESXI_BASE) as mock:
        mock.post(_SDK).mock(side_effect=router)
        payload = await connector._post_vmomi_json(
            target,
            "/HostDatastoreSystem/ha-datastoresystem/CreateNasDatastore",
            operator=_make_operator(),
            json={
                "spec": {
                    "_typeName": "HostNasVolumeSpec",
                    "remoteHost": "nas01.example.invalid",
                    "remotePath": "/export/nfs",
                    "localPath": "nfs-ds",
                    "accessMode": "readWrite",
                    "type": "NFS",
                }
            },
        )

    assert payload == {"type": "Datastore", "value": _DATASTORE_MOID}
    # The spec fields serialise into the envelope; _typeName is dropped (SOAP
    # announces types by position).
    body = router.bodies[-1]
    assert "<remoteHost>nas01.example.invalid</remoteHost>" in body
    assert "<localPath>nfs-ds</localPath>" in body
    assert "_typeName" not in body


@pytest.mark.asyncio
async def test_mark_as_ssd_task_routes_through_soap_and_returns_task_moref() -> None:
    """The disk_mark_flash caller's MarkAsSsd_Task rides SOAP → a Task MoRef to poll."""
    connector = _make_connector()
    _patch_no_revoke_aclose(connector)
    router = _SdkRouter()
    target = _esxi_fingerprinted()

    async with respx.mock(base_url=_ESXI_BASE) as mock:
        mock.post(_SDK).mock(side_effect=router)
        payload = await connector._post_vmomi_json(
            target,
            "/HostStorageSystem/ha-storagesystem/MarkAsSsd_Task",
            operator=_make_operator(),
            json={"scsiDiskUuid": "0200000000naa.6000c290"},
        )

    assert payload["type"] == "Task"
    assert payload["value"].startswith("haTask-")
    body = router.bodies[-1]
    assert "<scsiDiskUuid>0200000000naa.6000c290</scsiDiskUuid>" in body


@pytest.mark.asyncio
async def test_write_fault_raises_runtime_error_with_fault_type() -> None:
    """A non-auth vim write fault → RuntimeError carrying the fault type + faultstring."""
    connector = _make_connector()
    _patch_no_revoke_aclose(connector)
    target = _esxi_fingerprinted()

    def _fault_router(request: httpx.Request) -> httpx.Response:
        method = _soap_method(request.content.decode("utf-8"))
        if method == "RetrieveServiceContent":
            return httpx.Response(200, text=_SERVICE_CONTENT_XML)
        if method == "Login":
            return httpx.Response(
                200,
                text=_LOGIN_OK_XML,
                headers={"set-cookie": f"{_SOAP_COOKIE}={_COOKIE_VALUE}; Path=/"},
            )
        return httpx.Response(500, text=_HOST_CONFIG_FAULT_XML)  # CreateNasDatastore

    async with respx.mock(base_url=_ESXI_BASE) as mock:
        mock.post(_SDK).mock(side_effect=_fault_router)
        with pytest.raises(RuntimeError, match="HostConfigFault") as excinfo:
            await connector._post_vmomi_json(
                target,
                "/HostDatastoreSystem/ha-datastoresystem/CreateNasDatastore",
                operator=_make_operator(),
                json={"spec": {"remoteHost": "nas01.example.invalid"}},
            )

    # A write fault is not auth-class, so it does not become ConnectorAuthError.
    assert not isinstance(excinfo.value, ConnectorAuthError)
    assert "The NFS export is unreachable." in str(excinfo.value)


# ---------------------------------------------------------------------------
# Failure shapes + credential posture
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_invalid_login_fault_raises_connector_auth_error_hiding_credentials() -> None:
    """An InvalidLogin SOAP fault → ConnectorAuthError; the message hides the credential."""
    connector = _make_connector()
    _patch_no_revoke_aclose(connector)
    router = _SdkRouter(login_fault=True)

    async with respx.mock(base_url=_ESXI_BASE) as mock:
        mock.post(_SDK).mock(side_effect=router)
        with pytest.raises(ConnectorAuthError) as excinfo:
            await connector.auth_headers(_esxi_fingerprinted(), _make_operator())

    message = str(excinfo.value)
    assert "stub-password" not in message
    assert "svc-meho" not in message
    assert excinfo.value.status_code == 401


@pytest.mark.asyncio
async def test_login_without_cookie_is_establish_failure() -> None:
    """A 200 Login that sets no vmware_soap_session cookie is a clean establish failure."""
    connector = _make_connector()
    _patch_no_revoke_aclose(connector)

    def _no_cookie(request: httpx.Request) -> httpx.Response:
        body = request.content.decode("utf-8")
        if _soap_method(body) == "RetrieveServiceContent":
            return httpx.Response(200, text=_SERVICE_CONTENT_XML)
        return httpx.Response(200, text=_LOGIN_OK_XML)  # no Set-Cookie

    async with respx.mock(base_url=_ESXI_BASE) as mock:
        mock.post(_SDK).mock(side_effect=_no_cookie)
        with pytest.raises(RuntimeError, match="without a vmware_soap_session cookie"):
            await connector.auth_headers(_esxi_fingerprinted(), _make_operator())


# ---------------------------------------------------------------------------
# vCenter regression — a genuine 400 must NOT take the SOAP branch
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_non_jsonrpc_400_does_not_select_soap_branch() -> None:
    """A vCenter 400 without the JSON-RPC signature stays on the vAPI path (RuntimeError)."""
    connector = _make_connector()
    _patch_no_revoke_aclose(connector)
    router = _SdkRouter()

    async with respx.mock(base_url=_ESXI_BASE, assert_all_called=False) as mock:
        mock.post("/api/session").respond(400, text="<html>Bad Request</html>")
        mock.post(_SDK).mock(side_effect=router)
        with pytest.raises(RuntimeError, match="POST /api/session returned HTTP 400"):
            await connector.auth_headers(_StubTarget(), _make_operator())

    # The SOAP establish was never taken for a non-JSON-RPC 400.
    assert router.methods == []


# ---------------------------------------------------------------------------
# fingerprint() over the SOAP session
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fingerprint_standalone_esxi_reachable_product_esxi() -> None:
    """probe/fingerprint against a standalone ESXi 9.1 host → reachable=True, product=esxi."""
    connector = _make_connector()
    _patch_no_revoke_aclose(connector)
    router = _SdkRouter()

    async with respx.mock(base_url=_ESXI_BASE) as mock:
        mock.post("/api/session").respond(400, json=_JSONRPC_400_BODY)
        mock.post(_SDK).mock(side_effect=router)
        # ESXi's JSON-RPC handler answers GET /api/about with HTTP 400 (empty).
        mock.get("/api/about").respond(400)
        result = await connector.fingerprint(_StubTarget(), _make_operator())

    assert result.reachable is True
    assert result.vendor == "vmware"
    assert result.product == "esxi"
    assert result.version == _ABOUT_VERSION
    assert result.extras["session_flavor"] == "esxi"
    assert result.extras["api_type"] == "HostAgent"
    assert "soap-retrieveservicecontent" in result.probe_method


@pytest.mark.asyncio
async def test_probe_standalone_esxi_ok_true() -> None:
    """probe() folds the reachable ESXi fingerprint into ok=True."""
    connector = _make_connector()
    _patch_no_revoke_aclose(connector)
    router = _SdkRouter()

    async with respx.mock(base_url=_ESXI_BASE) as mock:
        mock.post(_SDK).mock(side_effect=router)
        mock.get("/api/about").respond(400)
        probe = await connector.probe(_esxi_fingerprinted())

    assert probe.ok is True


# ---------------------------------------------------------------------------
# Teardown + recovery
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_invalidate_session_logs_out_over_soap_and_drops_cache() -> None:
    """invalidate_session posts SOAP Logout on the SessionManager moid and clears the cache."""
    connector = _make_connector()
    _patch_no_revoke_aclose(connector)
    router = _SdkRouter()
    target = _esxi_fingerprinted()

    async with respx.mock(base_url=_ESXI_BASE) as mock:
        mock.post(_SDK).mock(side_effect=router)
        await connector.auth_headers(target, _make_operator())
        await connector.invalidate_session(target)

    assert router.methods == ["RetrieveServiceContent", "Login", "Logout"]
    logout_body = router.bodies[-1]
    assert f'<_this type="SessionManager">{_SM_MOID}</_this>' in logout_body
    # The cache slot is cleared so the next auth re-establishes.
    key = target_cache_key(target)
    assert key not in connector._session_tokens
    assert key not in connector._session_flavors
    assert key not in connector._esxi_session_manager_moids


@pytest.mark.asyncio
async def test_cold_re_login_after_invalidate() -> None:
    """invalidate_session → the next auth_headers cold-re-runs the SOAP establish."""
    connector = _make_connector()
    _patch_no_revoke_aclose(connector)
    router = _SdkRouter()
    target = _esxi_fingerprinted()

    async with respx.mock(base_url=_ESXI_BASE) as mock:
        mock.post(_SDK).mock(side_effect=router)
        await connector.auth_headers(target, _make_operator())  # establish #1
        await connector.invalidate_session(target)  # 401-recovery hook (+ Logout)
        await connector.auth_headers(target, _make_operator())  # establish #2 (cold)

    # ServiceContent+Login, then Logout, then ServiceContent+Login again.
    assert router.methods == [
        "RetrieveServiceContent",
        "Login",
        "Logout",
        "RetrieveServiceContent",
        "Login",
    ]


@pytest.mark.asyncio
async def test_invalidate_logout_failure_is_swallowed() -> None:
    """A failing Logout on invalidate never blocks the cold re-establish."""
    connector = _make_connector()
    _patch_no_revoke_aclose(connector)
    target = _esxi_fingerprinted()

    def _logout_down(request: httpx.Request) -> httpx.Response:
        body = request.content.decode("utf-8")
        method = _soap_method(body)
        if method == "RetrieveServiceContent":
            return httpx.Response(200, text=_SERVICE_CONTENT_XML)
        if method == "Login":
            return httpx.Response(
                200,
                text=_LOGIN_OK_XML,
                headers={"set-cookie": f"{_SOAP_COOKIE}={_COOKIE_VALUE}; Path=/"},
            )
        raise httpx.ConnectError("down")  # Logout

    async with respx.mock(base_url=_ESXI_BASE) as mock:
        mock.post(_SDK).mock(side_effect=_logout_down)
        await connector.auth_headers(target, _make_operator())
        # Must not raise even though Logout errors.
        await connector.invalidate_session(target)

    assert target_cache_key(target) not in connector._session_tokens


@pytest.mark.asyncio
async def test_aclose_logs_out_over_soap_not_delete() -> None:
    """aclose tears an ESXi session down with SOAP Logout, not DELETE /api/session."""
    connector = _make_connector()
    router = _SdkRouter()
    target = _esxi_fingerprinted()

    async with respx.mock(base_url=_ESXI_BASE, assert_all_called=False) as mock:
        mock.post(_SDK).mock(side_effect=router)
        api_delete = mock.delete("/api/session").respond(204)
        await connector.auth_headers(target, _make_operator())
        await connector.aclose()

    assert router.methods == ["RetrieveServiceContent", "Login", "Logout"]
    # The vAPI DELETE is never used for an ESXi-flavored session.
    assert api_delete.call_count == 0


# ---------------------------------------------------------------------------
# Flight-recorder span — the /sdk envelope never leaks into a captured span
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_soap_post_never_hands_envelope_to_flight_recorder(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The vendor-call span never serialises the /sdk SOAP envelope (Login <password>)."""
    connector = _make_connector()
    _patch_no_revoke_aclose(connector)
    router = _SdkRouter()
    target = _esxi_fingerprinted()

    recorded: list[dict[str, Any]] = []

    def _spy_record(start: Any, **kwargs: Any) -> None:
        recorded.append(kwargs)

    monkeypatch.setattr(connector_module.flight_recorder_capture, "record_vendor_call", _spy_record)

    async with respx.mock(base_url=_ESXI_BASE) as mock:
        mock.post(_SDK).mock(side_effect=router)
        await connector._post_vmomi_json(
            target,
            "/PropertyCollector/propertyCollector/RetrievePropertiesEx",
            operator=_make_operator(),
            json=build_host_storage_devices_retrieve_params("ha-host"),
        )

    # The span was recorded (observability parity with the vCenter path) ...
    assert recorded, "expected the vendor-call span to be recorded for /sdk posts"
    # ... but never with the SOAP envelope body, so the Login <password> (and
    # every other envelope) cannot leak into a captured span.
    for kwargs in recorded:
        assert kwargs.get("request_body") is None
        assert kwargs.get("request_content_type") is None
    # Defense-in-depth: the credential never appears anywhere in the spy args.
    blob = repr(recorded)
    assert "stub-password" not in blob
    assert "svc-meho" not in blob
