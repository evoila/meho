# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Unit tests for PBM SOAP session auth via the ``vcSessionCookie`` header (#3810).

The storage-policy composites ride the PBM SOAP service (``/pbm``, ``urn:pbm``).
vCenter's PBM endpoint authenticates a request by the **vim session cookie value
carried in a ``vcSessionCookie`` SOAP ``<Header>`` element** — NOT the HTTP
``vmware_soap_session`` cookie (which ``/pbm`` ignores) and NOT the vAPI token
the REST/VI-JSON path uses. ``_ensure_pbm`` mints a separate vim SOAP session on
``/sdk`` (``SessionManager.Login`` → the cookie value) and every PBM request
carries that value as the header (the hand-off pyvmomi's
``GetRequestContext()["vcSessionCookie"]`` and govmomi's ``soap.Client.Cookie``
perform).

``test_connectors_vmware_rest_soap_pbm`` covers the codec;
``test_connectors_vmware_rest_storage_policy`` fakes the seam at the
connector-method boundary. This module is the **transport** test: it drives the
real ``pbm_create_tag_profile`` / ``pbm_delete_profiles`` through a mocked
``/sdk`` + ``/pbm`` wire whose ``/pbm`` route **rejects** any authenticated
request lacking a ``vcSessionCookie`` equal to the cookie the mocked ``/sdk``
Login issued — so a regression that drops the header fails here (which is #3810:
the pre-fix connector sent only the HTTP cookie and the write faulted
``NotAuthenticated``).
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
import respx

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors._shared.vcf_auth import ConnectorAuthError
from meho_backplane.connectors.schemas import AuthModel
from meho_backplane.connectors.vmware_rest import VmwareRestConnector, VsphereTargetLike, soap_pbm
from meho_backplane.settings import get_settings

_VC_HOST = "vcenter-nested.test.invalid"
_VC_BASE = f"https://{_VC_HOST}"
_SDK = "/sdk"
_PBM = "/pbm"
_SOAP_COOKIE = "vmware_soap_session"

_SOAP_ENV = "http://schemas.xmlsoap.org/soap/envelope/"
_XSI = "http://www.w3.org/2001/XMLSchema-instance"

#: The authenticated PBM methods the mocked /pbm requires the header on; the
#: PbmRetrieveServiceContent bootstrap is lenient (models the live vCenter).
_PBM_AUTHED_METHODS = frozenset({"PbmCreate", "PbmDelete", "PbmRetrieveContent"})


def _envelope(inner: str, *, header: str = "") -> str:
    header_xml = f"<soapenv:Header>{header}</soapenv:Header>" if header else ""
    return (
        f'<soapenv:Envelope xmlns:soapenv="{_SOAP_ENV}" xmlns:xsi="{_XSI}">'
        f"{header_xml}<soapenv:Body>{inner}</soapenv:Body></soapenv:Envelope>"
    )


#: vim ``RetrieveServiceContent`` carrying only the ``sessionManager`` MoRef
#: ``_ensure_pbm`` reads (the vCenter singleton moid, not the ESXi ``ha-*``).
_VIM_SERVICE_CONTENT_XML = _envelope(
    '<RetrieveServiceContentResponse xmlns="urn:vim25"><returnval>'
    '<propertyCollector type="PropertyCollector">propertyCollector</propertyCollector>'
    '<sessionManager type="SessionManager">SessionManager</sessionManager>'
    "<about><version>9.1.0</version><apiVersion>9.1.0.0</apiVersion>"
    "<apiType>VirtualCenter</apiType></about>"
    "</returnval></RetrieveServiceContentResponse>"
)
_VIM_LOGIN_OK_XML = _envelope(
    '<LoginResponse xmlns="urn:vim25"><returnval><key>vc-session-key</key>'
    "<userName>administrator@custom.local</userName></returnval></LoginResponse>"
)


def _pbm_service_content_xml(profile_manager: str) -> str:
    """PBM ``PbmRetrieveServiceContent`` carrying the ``profileManager`` moid."""
    return _envelope(
        '<PbmRetrieveServiceContentResponse xmlns="urn:pbm"><returnval>'
        '<sessionManager type="PbmSessionManager">SessionManager</sessionManager>'
        f'<profileManager type="PbmProfileProfileManager">{profile_manager}</profileManager>'
        "</returnval></PbmRetrieveServiceContentResponse>"
    )


def _pbm_create_ok_xml(unique_id: str) -> str:
    return _envelope(
        f'<PbmCreateResponse xmlns="urn:pbm"><returnval><uniqueId>{unique_id}</uniqueId>'
        "</returnval></PbmCreateResponse>"
    )


def _pbm_delete_ok_xml() -> str:
    return _envelope('<PbmDeleteResponse xmlns="urn:pbm"/>')


def _fault_xml(fault_type: str, faultstring: str) -> str:
    """A vim ``<Fault>`` whose ``detail`` discriminator localName is *fault_type*."""
    return _envelope(
        "<soapenv:Fault><faultcode>ServerFaultCode</faultcode>"
        f"<faultstring>{faultstring}</faultstring>"
        f'<detail><{fault_type}Fault xsi:type="{fault_type}"></{fault_type}Fault></detail>'
        "</soapenv:Fault>"
    )


_NOT_AUTHENTICATED_FAULT_XML = _fault_xml("NotAuthenticated", "The session is not authenticated.")
_NO_PERMISSION_FAULT_XML = _fault_xml("NoPermission", "Permission to perform the operation denied.")
_NOT_AUTH = httpx.Response(500, text=_NOT_AUTHENTICATED_FAULT_XML)

_VC_SESSION_COOKIE_RE = re.compile(r"<vcSessionCookie>([^<]*)</vcSessionCookie>")


def _soap_method(body: str) -> str:
    """Return the SOAP method element name in a request envelope body."""
    for method in (
        "RetrieveServiceContent",
        "Login",
        "Logout",
        "PbmRetrieveServiceContent",
        "PbmCreate",
        "PbmDelete",
        "PbmRetrieveContent",
    ):
        if f"<{method} " in body or f"<{method}>" in body:
            return method
    return "?"


def _header_cookie(body: str) -> str | None:
    """The ``vcSessionCookie`` value in the request's SOAP header, or ``None``."""
    m = _VC_SESSION_COOKIE_RE.search(body)
    return m.group(1) if m else None


class _VcRouter:
    """A respx side-effect that replays the ``/sdk`` (vim) + ``/pbm`` wire.

    Each ``/sdk`` ``SessionManager.Login`` issues a **fresh** cookie value
    (``cookie-1``, ``cookie-2``, …) and records it as live; the ``/pbm`` route
    authenticates an *authenticated* method (``PbmCreate`` / ``PbmDelete`` /
    ``PbmRetrieveContent``) by requiring its ``vcSessionCookie`` SOAP header to
    equal a live cookie — a missing / wrong header faults ``NotAuthenticated``
    (the #3810 defect). The ``PbmRetrieveServiceContent`` bootstrap is lenient
    (accepts regardless, as the live vCenter does) but its header is still
    recorded so a test can assert the connector emits it there too.
    ``create_responses`` / ``delete_responses`` script the response **for
    header-authenticated attempts** so a mid-session expiry (a valid header that
    still faults) can be modelled.
    """

    def __init__(
        self,
        *,
        profile_managers: list[str] | None = None,
        create_responses: list[httpx.Response] | None = None,
        delete_responses: list[httpx.Response] | None = None,
    ) -> None:
        self.methods: list[str] = []
        self.pbm_header_cookies: list[tuple[str, str | None]] = []
        self.create_profile_managers: list[str] = []
        self.delete_profile_managers: list[str] = []
        self._profile_managers = profile_managers or ["ProfileManager"]
        self._service_content_calls = 0
        self._create_responses = create_responses or []
        self._create_calls = 0
        self._delete_responses = delete_responses or []
        self._delete_calls = 0
        self._logins = 0
        self._live_cookies: set[str] = set()

    @staticmethod
    def _this_moid(body: str) -> str:
        start = body.find("<_this")
        close = body.find(">", start)
        end = body.find("</_this>", close)
        return body[close + 1 : end]

    def sdk(self, request: httpx.Request) -> httpx.Response:
        method = _soap_method(request.content.decode("utf-8"))
        self.methods.append(method)
        if method == "RetrieveServiceContent":
            return httpx.Response(200, text=_VIM_SERVICE_CONTENT_XML)
        if method == "Login":
            self._logins += 1
            cookie = f"cookie-{self._logins}"
            self._live_cookies.add(cookie)
            return httpx.Response(
                200,
                text=_VIM_LOGIN_OK_XML,
                # Quote the value to exercise the connector's defensive unquote.
                headers={"set-cookie": f'{_SOAP_COOKIE}="{cookie}"; Path=/; HttpOnly'},
            )
        return httpx.Response(500, text="<unexpected-sdk/>")

    def pbm(self, request: httpx.Request) -> httpx.Response:
        body = request.content.decode("utf-8")
        method = _soap_method(body)
        self.methods.append(method)
        header_cookie = _header_cookie(body)
        self.pbm_header_cookies.append((method, header_cookie))
        # #3810: an authenticated PBM method MUST present a live vcSessionCookie.
        if method in _PBM_AUTHED_METHODS and header_cookie not in self._live_cookies:
            return _NOT_AUTH
        if method == "PbmRetrieveServiceContent":
            idx = min(self._service_content_calls, len(self._profile_managers) - 1)
            self._service_content_calls += 1
            return httpx.Response(200, text=_pbm_service_content_xml(self._profile_managers[idx]))
        if method == "PbmCreate":
            self.create_profile_managers.append(self._this_moid(body))
            resp = self._create_responses[min(self._create_calls, len(self._create_responses) - 1)]
            self._create_calls += 1
            return resp
        if method == "PbmDelete":
            self.delete_profile_managers.append(self._this_moid(body))
            resp = self._delete_responses[min(self._delete_calls, len(self._delete_responses) - 1)]
            self._delete_calls += 1
            return resp
        return httpx.Response(500, text="<unexpected-pbm/>")


def _make_operator() -> Operator:
    return Operator(
        sub="op-3810",
        name=None,
        email=None,
        raw_jwt="op.test.jwt",
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
    """Satisfies ``VsphereTargetLike`` structurally; ``fingerprint=None`` -> vCenter."""

    name: str = "vcenter-nested"
    host: str = _VC_HOST
    port: int | None = 443
    secret_ref: str = "vsphere/vcenter-nested"
    auth_model: str | None = AuthModel.SHARED_SERVICE_ACCOUNT.value
    tls_server_name: str | None = None
    fingerprint: dict[str, Any] | None = None
    id: UUID = field(default_factory=uuid4)
    tenant_id: UUID = field(default_factory=lambda: UUID(int=0))


async def _stub_loader(_target: VsphereTargetLike, _operator: Operator) -> dict[str, str]:
    return {"username": "administrator@custom.local", "password": "stub-password"}


def _make_connector() -> VmwareRestConnector:
    return VmwareRestConnector(session_loader=_stub_loader)


async def _close(connector: VmwareRestConnector) -> None:
    """Close pooled clients without a revoke leg (no vim token was cached)."""
    for client in connector._clients.values():
        await client.aclose()
    connector._clients.clear()


async def _create(connector: VmwareRestConnector) -> str:
    return await connector.pbm_create_tag_profile(
        _StubTarget(),
        _make_operator(),
        name="NFS-Gold",
        description="tag policy",
        category_name="meho-storage",
        tag_names=["nfs-gold"],
    )


# ---------------------------------------------------------------------------
# #3810 — the vcSessionCookie header authenticates the PBM write
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pbm_create_authenticates_with_vcsessioncookie_header() -> None:
    """PbmCreate carries the vim cookie as the vcSessionCookie header and succeeds first try."""
    connector = _make_connector()
    router = _VcRouter(create_responses=[httpx.Response(200, text=_pbm_create_ok_xml("policy-1"))])

    async with respx.mock(base_url=_VC_BASE) as mock:
        mock.post(_SDK).mock(side_effect=router.sdk)
        mock.post(_PBM).mock(side_effect=router.pbm)
        policy_id = await _create(connector)
    await _close(connector)

    assert policy_id == "policy-1"
    # One bootstrap + one create, no re-mint.
    assert router.methods == [
        "RetrieveServiceContent",
        "Login",
        "PbmRetrieveServiceContent",
        "PbmCreate",
    ]
    # Every /pbm request — bootstrap AND write — carried the issued cookie
    # (unquoted from the Set-Cookie value) as the vcSessionCookie header.
    assert router.pbm_header_cookies == [
        ("PbmRetrieveServiceContent", "cookie-1"),
        ("PbmCreate", "cookie-1"),
    ]


@pytest.mark.asyncio
async def test_pbm_create_without_the_header_faults_notauthenticated() -> None:
    """Drop the vcSessionCookie header (the pre-fix behaviour) -> the write is rejected (#3810).

    Proves the mock's /pbm auth is real and the header is load-bearing: with the
    header suppressed, the create fails on both the first attempt and the re-mint
    retry, surfacing the connector's PBM-transport-defect RuntimeError.
    """
    connector = _make_connector()
    router = _VcRouter(create_responses=[httpx.Response(200, text=_pbm_create_ok_xml("policy-1"))])

    async with respx.mock(base_url=_VC_BASE) as mock:
        mock.post(_SDK).mock(side_effect=router.sdk)
        mock.post(_PBM).mock(side_effect=router.pbm)
        # Simulate the pre-#3810 connector: no vcSessionCookie header emitted.
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(soap_pbm, "build_vc_session_cookie_header", lambda _cookie: "")
            with pytest.raises(RuntimeError) as exc_info:
                await _create(connector)
    await _close(connector)

    assert not isinstance(exc_info.value, ConnectorAuthError)
    assert "vcSessionCookie" in str(exc_info.value)
    assert "do NOT restage" in str(exc_info.value)
    # The write was attempted twice (initial + one re-mint) and both lacked the
    # header (header_cookie is None), which is exactly what /pbm rejected.
    assert [c for m, c in router.pbm_header_cookies if m == "PbmCreate"] == [None, None]
    assert router.methods.count("PbmCreate") == 2
    assert router.methods.count("Login") == 2


# ---------------------------------------------------------------------------
# Session-expiry self-heal (a valid header that still faults -> re-login once)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pbm_create_self_heals_expired_session_once() -> None:
    """A valid-header PbmCreate that still faults NotAuthenticated re-logs in and retries."""
    connector = _make_connector()
    router = _VcRouter(
        # The re-login advertises a fresh profileManager moid; the retry must
        # rebuild the envelope against it and against the fresh cookie.
        profile_managers=["ProfileManager-A", "ProfileManager-B"],
        create_responses=[
            _NOT_AUTH,  # attempt 1: header valid, but session expired server-side
            httpx.Response(200, text=_pbm_create_ok_xml("policy-after-relogin")),
        ],
    )

    async with respx.mock(base_url=_VC_BASE) as mock:
        mock.post(_SDK).mock(side_effect=router.sdk)
        mock.post(_PBM).mock(side_effect=router.pbm)
        policy_id = await _create(connector)
    await _close(connector)

    assert policy_id == "policy-after-relogin"
    assert router.methods == [
        "RetrieveServiceContent",
        "Login",
        "PbmRetrieveServiceContent",
        "PbmCreate",
        "RetrieveServiceContent",
        "Login",
        "PbmRetrieveServiceContent",
        "PbmCreate",
    ]
    # The retried create used the fresh profileManager moid AND the fresh cookie.
    assert router.create_profile_managers == ["ProfileManager-A", "ProfileManager-B"]
    assert [c for m, c in router.pbm_header_cookies if m == "PbmCreate"] == ["cookie-1", "cookie-2"]


@pytest.mark.asyncio
async def test_pbm_delete_self_heals_expired_session_once() -> None:
    """The self-heal covers PbmDelete too (all PBM methods funnel through _pbm_call)."""
    connector = _make_connector()
    router = _VcRouter(
        delete_responses=[_NOT_AUTH, httpx.Response(200, text=_pbm_delete_ok_xml())],
    )

    async with respx.mock(base_url=_VC_BASE) as mock:
        mock.post(_SDK).mock(side_effect=router.sdk)
        mock.post(_PBM).mock(side_effect=router.pbm)
        outcomes = await connector.pbm_delete_profiles(
            _StubTarget(), _make_operator(), profile_ids=["policy-1"]
        )
    await _close(connector)

    assert outcomes == []  # empty PbmDelete returnval => removed without a per-id fault
    assert router.methods.count("PbmDelete") == 2
    assert router.methods.count("Login") == 2
    assert [c for m, c in router.pbm_header_cookies if m == "PbmDelete"] == ["cookie-1", "cookie-2"]


# ---------------------------------------------------------------------------
# Persistent NotAuthenticated with the header present -> transport defect
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pbm_create_persistent_notauthenticated_is_a_transport_defect() -> None:
    """Header present but /pbm keeps rejecting -> non-auth RuntimeError, do-not-restage."""
    connector = _make_connector()
    router = _VcRouter(create_responses=[_NOT_AUTH])

    async with respx.mock(base_url=_VC_BASE) as mock:
        mock.post(_SDK).mock(side_effect=router.sdk)
        mock.post(_PBM).mock(side_effect=router.pbm)
        with pytest.raises(RuntimeError) as exc_info:
            await _create(connector)
    await _close(connector)

    # A stale/rotated credential would surface as ConnectorAuthError (restage
    # remediation); the header is present and vim25/REST authenticate with the
    # same secret, so this is a plain RuntimeError naming a transport/auth defect.
    assert not isinstance(exc_info.value, ConnectorAuthError)
    message = str(exc_info.value)
    assert "vcSessionCookie" in message
    assert "do NOT restage" in message
    assert "transport/auth defect" in message
    # The header WAS present on both attempts — this is not a missing-header case.
    assert [c for m, c in router.pbm_header_cookies if m == "PbmCreate"] == ["cookie-1", "cookie-2"]


# ---------------------------------------------------------------------------
# Genuine credential/privilege rejection is NOT retried
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pbm_create_no_permission_fault_is_not_retried() -> None:
    """A NoPermission fault is a genuine rejection: raise ConnectorAuthError, no re-login."""
    connector = _make_connector()
    router = _VcRouter(create_responses=[httpx.Response(500, text=_NO_PERMISSION_FAULT_XML)])

    async with respx.mock(base_url=_VC_BASE) as mock:
        mock.post(_SDK).mock(side_effect=router.sdk)
        mock.post(_PBM).mock(side_effect=router.pbm)
        with pytest.raises(ConnectorAuthError) as exc_info:
            await _create(connector)
    await _close(connector)

    assert exc_info.value.status_code == 401
    # No re-login: one Login, one PbmCreate attempt.
    assert router.methods.count("Login") == 1
    assert router.methods.count("PbmCreate") == 1
    # The message names the method+target and never echoes the credential.
    assert "PbmCreate" in str(exc_info.value)
    assert "stub-password" not in str(exc_info.value)
