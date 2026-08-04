# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Unit tests for :class:`VmwareRestConnector` fingerprint + probe (G3.1-T1 #498).

Coverage matrix (per #498 acceptance criteria):

* :func:`product_from_line_id` maps ``vpx``->``vcenter``,
  ``embeddedEsx``->``esxi``, ``esx``->``esxi``; falls through to the raw
  line_id for unknown values; defends against ``""`` / ``None``.
* :meth:`fingerprint` returns canonical :class:`FingerprintResult` for a
  ``GET /api/about`` response with vendor / product / version / build /
  edition + the full ``extras`` shape (uuid, full_name, product_line_id,
  api_type, os_type).
* :meth:`fingerprint` returns ``reachable=False`` with the exception
  class + message on transport / connect / TLS / status failure (TCP
  ``ConnectError``, HTTP 401 from session, 5xx from ``/api/about``).
* :meth:`probe` returns ``ok=True`` when fingerprint is reachable;
  ``ok=False`` with ``reason`` populated when not.

Plus the #2765 vCenter-capable fallback chain (``GET /api/about`` is the
ESXi host REST surface; vCenter 404s it):

* a 404 on ``/api/about`` falls back to the unauthenticated
  ``/sdk/vimServiceVersions.xml`` (``reachable=True`` + vim25 API
  version), enriched by ``GET /api/appliance/system/version`` when the
  appliance surface answers (``product="vcenter"`` + version/build);
* both probes failing stays ``reachable=False`` with the error in
  ``extras``; non-404 ``/api/about`` failures never enter the fallback;
* the unreachable arm no longer hardcodes ``product="vcenter"`` — it
  reports the target's registered product, else ``"unknown"``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
import respx

from meho_backplane.auth.operator import Operator
from meho_backplane.connectors.schemas import (
    AuthModel,
    FingerprintResult,
    ProbeResult,
)
from meho_backplane.connectors.vmware_rest import (
    VmwareRestConnector,
    VsphereTargetLike,
    product_from_line_id,
)
from meho_backplane.connectors.vmware_rest.connector import service_versions_api_version

# ---------------------------------------------------------------------------
# Target stub — same shape as the auth-test module's stub
# ---------------------------------------------------------------------------


@dataclass
class _StubTarget:
    name: str
    host: str
    port: int | None
    secret_ref: str
    auth_model: str | None = AuthModel.SHARED_SERVICE_ACCOUNT.value
    # Tenant-unique cache key components (#1642/#1672).
    id: UUID = field(default_factory=uuid4)
    tenant_id: UUID = field(default_factory=lambda: UUID(int=0))


_TARGET = _StubTarget(
    name="vcenter-fp",
    host="vcenter-fp.test.invalid",
    port=443,
    secret_ref="vsphere/vcenter-fp",
)


async def _stub_loader(_target: VsphereTargetLike, _operator: Operator) -> dict[str, str]:
    return {"username": "svc-meho", "password": "stub-password"}


def _make_connector() -> VmwareRestConnector:
    return VmwareRestConnector(session_loader=_stub_loader)


def _patch_no_revoke_aclose(connector: VmwareRestConnector) -> None:
    async def _aclose() -> None:
        connector._session_tokens.clear()
        for client in connector._clients.values():
            await client.aclose()
        connector._clients.clear()

    connector.aclose = _aclose  # type: ignore[method-assign]


def _about_payload() -> dict[str, Any]:
    """Sample /api/about response shape (modelled on real vCenter 8 output)."""
    return {
        "product_line_id": "vpx",
        "version": "9.0.0.10000",
        "build": "12345678",
        "license_product_name": "VMware vCenter Server Standard",
        "instance_uuid": "abcdef01-2345-6789-abcd-ef0123456789",
        "full_name": "VMware vCenter Server 9.0.0",
        "api_type": "VirtualCenter",
        "os_type": "linux-x64",
    }


# ---------------------------------------------------------------------------
# product_from_line_id mapping
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("line_id", "expected"),
    [
        ("vpx", "vcenter"),
        ("embeddedEsx", "esxi"),
        ("esx", "esxi"),
        # Fall-through preserves the raw line_id for forward-compat
        # with future product flavours we don't yet have a canonical
        # mapping for.
        ("some-future-product", "some-future-product"),
        # Empty / missing line_id falls through to "unknown" so the
        # fingerprint never carries a misleading product slug.
        ("", "unknown"),
    ],
)
def test_product_from_line_id_mapping(line_id: str, expected: str) -> None:
    assert product_from_line_id(line_id) == expected


# ---------------------------------------------------------------------------
# fingerprint — happy path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fingerprint_builds_canonical_shape_for_vcenter() -> None:
    """A live /api/about response is folded into the canonical FingerprintResult."""
    connector = _make_connector()
    _patch_no_revoke_aclose(connector)

    async with respx.mock(base_url="https://vcenter-fp.test.invalid") as mock:
        mock.post("/api/session").respond(200, json="fingerprint-session-token")
        mock.get("/api/about").respond(200, json=_about_payload())
        result = await connector.fingerprint(_TARGET)

    assert isinstance(result, FingerprintResult)
    assert result.vendor == "vmware"
    assert result.product == "vcenter"  # product_line_id="vpx" -> "vcenter"
    assert result.version == "9.0.0.10000"
    assert result.build == "12345678"
    assert result.edition == "VMware vCenter Server Standard"
    assert result.reachable is True
    assert result.probe_method == "GET /api/about"
    assert dict(result.extras) == {
        "uuid": "abcdef01-2345-6789-abcd-ef0123456789",
        "full_name": "VMware vCenter Server 9.0.0",
        "product_line_id": "vpx",
        "api_type": "VirtualCenter",
        "os_type": "linux-x64",
    }
    await connector.aclose()


@pytest.mark.asyncio
async def test_fingerprint_maps_esxi_product_line_id() -> None:
    """An ESXi target's product_line_id maps to product=esxi."""
    connector = _make_connector()
    _patch_no_revoke_aclose(connector)

    payload = _about_payload()
    payload["product_line_id"] = "embeddedEsx"
    payload["api_type"] = "HostAgent"

    async with respx.mock(base_url="https://vcenter-fp.test.invalid") as mock:
        mock.post("/api/session").respond(200, json="esxi-session-token")
        mock.get("/api/about").respond(200, json=payload)
        result = await connector.fingerprint(_TARGET)

    assert result.product == "esxi"
    assert result.extras["product_line_id"] == "embeddedEsx"
    assert result.extras["api_type"] == "HostAgent"
    await connector.aclose()


# ---------------------------------------------------------------------------
# fingerprint — failure modes
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fingerprint_returns_unreachable_on_connect_error() -> None:
    """A TCP ConnectError surfaces as reachable=False with the error in extras."""
    connector = _make_connector()
    _patch_no_revoke_aclose(connector)

    async with respx.mock(base_url="https://vcenter-fp.test.invalid") as mock:
        # The session POST itself fails — the connector wraps that into
        # a RuntimeError which fingerprint() catches.
        mock.post("/api/session").mock(side_effect=httpx.ConnectError("refused"))
        result = await connector.fingerprint(_TARGET)

    assert result.reachable is False
    # Vendor stays vmware so the operator can identify the failed
    # connector class without a separate lookup.
    assert result.vendor == "vmware"
    # #2765: the unreachable arm no longer asserts product="vcenter" —
    # nothing was observed, and the stub target carries no registered
    # product, so the truthful value is "unknown".
    assert result.product == "unknown"
    assert result.probe_method == "GET /api/about"
    assert "ConnectError" in result.extras["error"]
    await connector.aclose()


@pytest.mark.asyncio
async def test_fingerprint_returns_unreachable_on_session_401() -> None:
    """A 401 on POST /api/session surfaces as reachable=False."""
    connector = _make_connector()
    _patch_no_revoke_aclose(connector)

    async with respx.mock(base_url="https://vcenter-fp.test.invalid") as mock:
        mock.post("/api/session").respond(401)
        result = await connector.fingerprint(_TARGET)

    assert result.reachable is False
    assert result.product == "unknown"  # #2765: no observed product on failure
    assert "401" in result.extras["error"]
    await connector.aclose()


@pytest.mark.asyncio
async def test_fingerprint_returns_unreachable_on_about_5xx() -> None:
    """A 5xx from /api/about (after a successful session POST) surfaces as not-reachable.

    The HttpConnector retries 5xx 3 times before re-raising; we wait
    for the retry exhaustion by patching tenacity's wait to a no-op.
    """
    from unittest.mock import patch

    from tenacity import wait_none

    connector = _make_connector()
    _patch_no_revoke_aclose(connector)

    async with respx.mock(base_url="https://vcenter-fp.test.invalid") as mock:
        mock.post("/api/session").respond(200, json="ok-token")
        mock.get("/api/about").respond(503)
        with patch.object(
            connector._request_json.retry,  # type: ignore[attr-defined]
            "wait",
            wait_none(),
        ):
            result = await connector.fingerprint(_TARGET)

    assert result.reachable is False
    assert "503" in result.extras["error"]
    await connector.aclose()


# ---------------------------------------------------------------------------
# #2765 — vCenter-capable fallback chain
# ---------------------------------------------------------------------------

#: The version-discovery document both vCenter and ESXi serve
#: unauthenticated at /sdk/vimServiceVersions.xml (shape per pyVim's
#: ``__VersionIsSupported`` parser and govmomi's simulator template).
_SERVICE_VERSIONS_XML = """<?xml version="1.0" encoding="UTF-8"?>
<namespaces version="1.0">
 <namespace>
  <name>urn:vim25</name>
  <version>8.0.3.0</version>
  <priorVersions>
   <version>8.0.2.0</version>
   <version>7.0.3.0</version>
  </priorVersions>
 </namespace>
</namespaces>
"""

#: ``GET /api/appliance/system/version`` body (appliance VersionStruct
#: shape per the vSphere Automation SDK: version / product / build /
#: type / summary / releasedate / install_time).
_APPLIANCE_VERSION_PAYLOAD: dict[str, Any] = {
    "version": "8.0.3.00000",
    "build": "24022515",
    "product": "VMware vCenter Server",
    "type": "vCenter Server with an embedded Platform Services Controller",
    "summary": "Patch for VMware vCenter Server 8.0",
    "releasedate": "May 21, 2024",
    "install_time": "2024-06-01T00:00:00.000Z",
}


@pytest.mark.asyncio
async def test_fingerprint_falls_back_to_service_versions_on_about_404() -> None:
    """A 404 on /api/about + a served discovery document → reachable=True.

    The vCenter shape (#2765): no /api/about, but the unauthenticated
    version-discovery document answers. Without the appliance surface
    (404 here) the fingerprint carries the vim25 API version and the
    truthful probe_method; the stub target registers no product, so
    product falls back to "unknown".
    """
    connector = _make_connector()
    _patch_no_revoke_aclose(connector)

    async with respx.mock(base_url="https://vcenter-fp.test.invalid") as mock:
        mock.post("/api/session").respond(200, json="vc-session-token")
        mock.get("/api/about").respond(404)
        mock.get("/sdk/vimServiceVersions.xml").respond(
            200, text=_SERVICE_VERSIONS_XML, headers={"content-type": "text/xml"}
        )
        mock.get("/api/appliance/system/version").respond(404)
        result = await connector.fingerprint(_TARGET)

    assert result.reachable is True
    assert result.vendor == "vmware"
    assert result.product == "unknown"
    assert result.version == "8.0.3.0"
    assert result.build is None
    assert result.probe_method == "GET /sdk/vimServiceVersions.xml"
    assert result.extras["api_version"] == "8.0.3.0"
    await connector.aclose()


@pytest.mark.asyncio
async def test_fingerprint_fallback_reports_registered_product_when_present() -> None:
    """Without observed product evidence the target's registered product is used."""

    @dataclass
    class _ProductTarget(_StubTarget):
        product: str = "vmware"

    target = _ProductTarget(
        name="vcenter-fp",
        host="vcenter-fp.test.invalid",
        port=443,
        secret_ref="vsphere/vcenter-fp",
    )
    connector = _make_connector()
    _patch_no_revoke_aclose(connector)

    async with respx.mock(base_url="https://vcenter-fp.test.invalid") as mock:
        mock.post("/api/session").respond(200, json="vc-session-token")
        mock.get("/api/about").respond(404)
        mock.get("/sdk/vimServiceVersions.xml").respond(200, text=_SERVICE_VERSIONS_XML)
        mock.get("/api/appliance/system/version").respond(404)
        result = await connector.fingerprint(target)

    assert result.reachable is True
    assert result.product == "vmware"
    await connector.aclose()


@pytest.mark.asyncio
async def test_fingerprint_fallback_enriches_from_appliance_version() -> None:
    """With a session, the VCSA appliance read supplies version + build.

    The appliance surface is vCenter-only, so an answer is observed
    evidence for product="vcenter"; version/build come from the
    authoritative appliance VersionStruct, and probe_method names both
    endpoints that produced the result.
    """
    connector = _make_connector()
    _patch_no_revoke_aclose(connector)

    async with respx.mock(base_url="https://vcenter-fp.test.invalid") as mock:
        mock.post("/api/session").respond(200, json="vc-session-token")
        mock.get("/api/about").respond(404)
        mock.get("/sdk/vimServiceVersions.xml").respond(200, text=_SERVICE_VERSIONS_XML)
        mock.get("/api/appliance/system/version").respond(200, json=_APPLIANCE_VERSION_PAYLOAD)
        result = await connector.fingerprint(_TARGET)

    assert result.reachable is True
    assert result.product == "vcenter"
    assert result.version == "8.0.3.00000"
    assert result.build == "24022515"
    assert result.probe_method == (
        "GET /sdk/vimServiceVersions.xml + GET /api/appliance/system/version"
    )
    assert result.extras["api_version"] == "8.0.3.0"
    assert result.extras["product_name"] == "VMware vCenter Server"
    assert result.extras["type"] == ("vCenter Server with an embedded Platform Services Controller")
    await connector.aclose()


@pytest.mark.asyncio
async def test_fingerprint_stays_unreachable_when_both_probes_fail() -> None:
    """/api/about 404 + discovery document 404 → reachable=False with the error."""
    connector = _make_connector()
    _patch_no_revoke_aclose(connector)

    async with respx.mock(base_url="https://vcenter-fp.test.invalid") as mock:
        mock.post("/api/session").respond(200, json="vc-session-token")
        mock.get("/api/about").respond(404)
        mock.get("/sdk/vimServiceVersions.xml").respond(404)
        result = await connector.fingerprint(_TARGET)

    assert result.reachable is False
    assert result.product == "unknown"
    assert result.probe_method == "GET /api/about + GET /sdk/vimServiceVersions.xml"
    assert "404" in result.extras["error"]
    assert "404" in result.extras["api_about"]
    await connector.aclose()


@pytest.mark.asyncio
async def test_fingerprint_unreachable_when_discovery_document_unparsable() -> None:
    """A 200 with no vim25 version (some other web server) is not reachable."""
    connector = _make_connector()
    _patch_no_revoke_aclose(connector)

    async with respx.mock(base_url="https://vcenter-fp.test.invalid") as mock:
        mock.post("/api/session").respond(200, json="vc-session-token")
        mock.get("/api/about").respond(404)
        mock.get("/sdk/vimServiceVersions.xml").respond(200, text="<html>login page</html>")
        result = await connector.fingerprint(_TARGET)

    assert result.reachable is False
    assert "no vim25 API version" in result.extras["error"]
    await connector.aclose()


@pytest.mark.parametrize(
    ("document", "expected"),
    [
        # The modern un-namespaced shape (govmomi simulator template);
        # priorVersions entries must not shadow the current version.
        (_SERVICE_VERSIONS_XML, "8.0.3.0"),
        # The XML-namespaced variant older products serve (the second
        # format pyVim's __VersionIsSupported handles).
        (
            '<namespaces version="1.0" xmlns="http://www.vmware.com/vi/versions">'
            "<namespace><name>urn:vim25</name><version>6.7.3</version></namespace>"
            "</namespaces>",
            "6.7.3",
        ),
        # vim25 entry wins over other namespace entries regardless of order.
        (
            '<namespaces version="1.0">'
            "<namespace><name>urn:vim</name><version>2.5</version></namespace>"
            "<namespace><name>urn:vim25</name><version>7.0.3.0</version></namespace>"
            "</namespaces>",
            "7.0.3.0",
        ),
        # No vim25 entry: fall back to the first entry carrying a version.
        (
            '<namespaces version="1.0">'
            "<namespace><name>urn:vim</name><version>2.5</version></namespace>"
            "</namespaces>",
            "2.5",
        ),
        # No namespace entries / not the discovery document at all.
        ('<namespaces version="1.0"></namespaces>', None),
        ("<html>login page</html>", None),
        # Malformed XML must yield None, never raise.
        ("<namespaces><namespace>", None),
        ("", None),
    ],
)
def test_service_versions_api_version_parsing(document: str, expected: str | None) -> None:
    assert service_versions_api_version(document) == expected


# ---------------------------------------------------------------------------
# probe
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_probe_returns_ok_when_fingerprint_succeeds() -> None:
    """probe()==ok=True when fingerprint() returns reachable=True."""
    connector = _make_connector()
    _patch_no_revoke_aclose(connector)

    async with respx.mock(base_url="https://vcenter-fp.test.invalid") as mock:
        mock.post("/api/session").respond(200, json="probe-token")
        mock.get("/api/about").respond(200, json=_about_payload())
        result = await connector.probe(_TARGET)

    assert isinstance(result, ProbeResult)
    assert result.ok is True
    assert result.reason is None
    await connector.aclose()


@pytest.mark.asyncio
async def test_probe_returns_not_ok_on_connect_error() -> None:
    """probe()==ok=False with the error message populated when transport fails."""
    connector = _make_connector()
    _patch_no_revoke_aclose(connector)

    async with respx.mock(base_url="https://vcenter-fp.test.invalid") as mock:
        mock.post("/api/session").mock(side_effect=httpx.ConnectError("refused"))
        result = await connector.probe(_TARGET)

    assert result.ok is False
    assert result.reason is not None
    assert "ConnectError" in result.reason
    await connector.aclose()


@pytest.mark.asyncio
async def test_probe_returns_not_ok_on_auth_failure() -> None:
    """probe()==ok=False with the 401 surfaced in reason."""
    connector = _make_connector()
    _patch_no_revoke_aclose(connector)

    async with respx.mock(base_url="https://vcenter-fp.test.invalid") as mock:
        mock.post("/api/session").respond(401)
        result = await connector.probe(_TARGET)

    assert result.ok is False
    assert result.reason is not None
    assert "401" in result.reason
    await connector.aclose()


# ---------------------------------------------------------------------------
# G0.16-T4 (#1306) probe-vs-dispatch convergence
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fingerprint_forwards_route_operator_to_session_loader() -> None:
    """G0.16-T4 (#1306) probe-vs-dispatch convergence regression for vmware-rest.

    Pre-#1306 the probe route called ``cls().fingerprint(target)``
    without an operator; the connector synthesised a system operator
    whose placeholder ``raw_jwt`` ("system:connector-probe-placeholder-
    jwt") is not a compact-JWS. Vault's JWT/OIDC auth method rejected
    the placeholder before the per-target Vault read could fire,
    surfacing as ``vault OIDC malformed jwt: must have three parts``
    on the v0.8.0 dogfood's ``rdc-vcenter`` probe.

    Post-#1306 the probe route forwards its operator to ``fingerprint``,
    which threads it to the session loader — the same code path the
    dispatch surface uses. This test pins:

    1. The session loader receives the route operator (not the
       system-operator stand-in).
    2. The forwarded JWT has the compact-JWS shape (≥3 dot-separated
       parts, the issue body's acceptance criterion 4).
    """
    import uuid as _uuid

    from meho_backplane.auth.operator import TenantRole

    captured: list[Operator] = []

    async def _capturing_loader(
        target: VsphereTargetLike,
        operator: Operator,
    ) -> dict[str, str]:
        captured.append(operator)
        return {"username": "svc-meho", "password": "stub-password"}

    connector = VmwareRestConnector(session_loader=_capturing_loader)
    _patch_no_revoke_aclose(connector)

    route_operator = Operator(
        sub="op-rdc",
        name="RDC Operator",
        email=None,
        raw_jwt="header.payload.signature",
        tenant_id=_uuid.UUID("00000000-0000-0000-0000-00000000a0a0"),
        tenant_role=TenantRole.OPERATOR,
    )

    async with respx.mock(base_url="https://vcenter-fp.test.invalid") as mock:
        mock.post("/api/session").respond(200, json="session-token-xyz")
        mock.get("/api/about").respond(200, json=_about_payload())
        await connector.fingerprint(_TARGET, operator=route_operator)

    assert len(captured) == 1, (
        "the session loader must run during cold-cache fingerprint; one invocation expected"
    )
    fwd_operator = captured[0]
    assert fwd_operator.sub == route_operator.sub, (
        "the session loader saw a different identity than the route "
        "operator forwarded — this is the #1306 divergence"
    )
    # Compact-JWS sanity check — the acceptance criterion's literal.
    jwt_parts = fwd_operator.raw_jwt.split(".")
    assert len(jwt_parts) >= 3, (
        f"forwarded JWT does not look like a compact-JWS (got "
        f"{len(jwt_parts)} parts; expected ≥3) — Vault would reject this "
        "with ``malformed jwt: must have three parts``"
    )
    await connector.aclose()


@pytest.mark.asyncio
async def test_fingerprint_without_operator_falls_back_to_system_operator() -> None:
    """``fingerprint(target)`` without ``operator`` retains the system-
    operator fall-back (the system-call carve-out for readiness probes).
    """
    captured: list[Operator] = []

    async def _capturing_loader(
        target: VsphereTargetLike,
        operator: Operator,
    ) -> dict[str, str]:
        captured.append(operator)
        return {"username": "svc-meho", "password": "stub-password"}

    connector = VmwareRestConnector(session_loader=_capturing_loader)
    _patch_no_revoke_aclose(connector)

    async with respx.mock(base_url="https://vcenter-fp.test.invalid") as mock:
        mock.post("/api/session").respond(200, json="session-token-xyz")
        mock.get("/api/about").respond(200, json=_about_payload())
        await connector.fingerprint(_TARGET)

    assert len(captured) == 1
    assert captured[0].sub == "system:connector-probe", (
        "the legacy fall-back must synthesise the system operator"
    )
    await connector.aclose()
