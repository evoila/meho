# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Integration test for :class:`VmwareRestConnector` over a respx-mocked vCenter.

Exercises the live ``fingerprint`` / ``probe`` / session-cache /
``aclose`` paths of the connector against a respx-mocked modern
vCenter REST surface.

Why respx and not a real ``vmware/vcsim`` container
===================================================

This module used to boot ``vmware/vcsim`` via testcontainers. That is
**unsatisfiable for these assertions**: govmomi's vcsim does not
implement the vCenter REST *resource/appliance* API. ``GET /api/about``
(what :meth:`VmwareRestConnector.fingerprint` calls) 404s on vcsim —
it only stubs the vAPI session / tagging / content-library subset plus
the SOAP/SDK surface. The previous "``GET /api/about`` returns a
synthesised inventory shape" note was incorrect; the test had been red
on ``main`` for exactly this reason.

Per the decision recorded in evoila/meho#536 (and mirroring the
``tests/acceptance`` migration in #535), the connector is exercised
against a respx-mocked surface that reproduces the exact wire contract
``fingerprint`` / ``probe`` / session establishment / ``aclose`` rely
on. The full connector code path (session POST → cached token → ``GET
/api/about`` → ``FingerprintResult`` mapping → ``DELETE /api/session``
revoke) runs unchanged; only the transport is mocked. No Docker
dependency — respx runs in-process.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
import respx

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors._shared.cache_key import target_cache_key
from meho_backplane.connectors.schemas import AuthModel
from meho_backplane.connectors.vmware_rest import (
    VmwareRestConnector,
    VsphereTargetLike,
)

# ---------------------------------------------------------------------------
# Mocked vCenter surface
# ---------------------------------------------------------------------------

#: Base URL the target points at. Port 443 keeps
#: ``HttpConnector._base_url`` from appending ``:port`` so the respx
#: router's ``base_url`` matches the connector's client URL exactly.
#: ``.test.invalid`` (RFC 6761) guarantees no real egress.
VCENTER_BASE_URL: str = "https://vcsim-integration.test.invalid"

#: ``GET /api/about`` body. Shapes the :class:`FingerprintResult` the
#: connector builds: ``product_line_id="vpx"`` →
#: :func:`product_from_line_id` → ``"vcenter"``; the other keys flow
#: onto ``version`` / ``build`` / ``edition`` / ``extras``.
ABOUT_PAYLOAD: dict[str, Any] = {
    "product_line_id": "vpx",
    "version": "9.0.0.0",
    "build": "24021000",
    "license_product_name": "VMware vCenter Server",
    "instance_uuid": "b3f9f1a0-0000-4000-8000-0000000000ab",
    "full_name": "VMware vCenter Server 9.0.0.0 build-24021000",
    "api_type": "VirtualCenter",
    "os_type": "linux-x64",
}

#: Session token the mocked ``POST /api/session`` returns. vSphere
#: 8.0+/9.0 returns the token as a bare JSON string body; the
#: connector's ``_extract_session_token`` handles that shape.
SESSION_TOKEN: str = "integration-mock-session-token"

#: ``GET /vcenter/host`` listing (vCenter Automation REST). The
#: ``vmware.host.usage`` typed op lists hosts here, then reads per-host
#: utilisation via PropertyCollector. Bare-array modern shape.
HOST_LISTING: list[dict[str, Any]] = [
    {"host": "host-11", "name": "esx-a.test.invalid", "connection_state": "CONNECTED"},
    {"host": "host-22", "name": "esx-b.test.invalid", "connection_state": "CONNECTED"},
]

#: A ``RetrievePropertiesEx`` ``RetrieveResult`` carrying the three
#: WS-API host-usage properties the typed op requests. The respx handler
#: echoes the queried host's moId back into the ``obj`` so a single
#: static body serves every per-host POST.
_RETRIEVE_QUICK_STATS: dict[str, Any] = {
    "overallCpuUsage": 5200,
    "overallMemoryUsage": 131072,
    "uptime": 1728000,
}
_RETRIEVE_HARDWARE: dict[str, Any] = {
    "cpuModel": "Intel(R) Xeon(R) Gold 6248R",
    "cpuMhz": 3000,
    "numCpuPkgs": 2,
    "numCpuCores": 48,
    "numCpuThreads": 96,
    "memorySize": 274877906944,
}


def _retrieve_properties_response(request: httpx.Request) -> httpx.Response:
    """Build a ``RetrieveResult`` echoing the POSTed host moId.

    The typed op issues one RetrievePropertiesEx per host with the host's
    MoRef in ``specSet[0].objectSet[0].obj.value``; the handler reflects
    it back so the same static quickStats/hardware body serves every host.
    """
    body = json.loads(request.content)
    moid = body["specSet"][0]["objectSet"][0]["obj"]["value"]
    return httpx.Response(
        200,
        json={
            "objects": [
                {
                    "obj": {"type": "HostSystem", "value": moid},
                    "propSet": [
                        {"name": "summary.quickStats", "val": _RETRIEVE_QUICK_STATS},
                        {"name": "summary.hardware", "val": _RETRIEVE_HARDWARE},
                        {"name": "runtime.inMaintenanceMode", "val": moid == "host-22"},
                    ],
                }
            ]
        },
    )


def _register_vcenter_routes(mock: respx.MockRouter) -> None:
    """Register the modern vCenter REST surface the connector calls.

    ``POST /api/session`` (200 → token; the modern path succeeds so the
    connector records ``/api/session`` as the established path),
    ``GET /api/about`` (the fingerprint probe), ``DELETE /api/session``
    (the ``aclose`` revoke against the established path), plus the
    ``vmware.host.usage`` surface: ``GET /api/vcenter/host`` and the
    per-host vmomi ``POST /sdk/vim25/9.0.0.0/PropertyCollector/propertyCollector/
    RetrievePropertiesEx`` (the listing mounts onto ``/api`` via
    :meth:`VmwareRestConnector.mount_op_path`; the vmomi read mounts onto the
    documented VI-JSON ``/sdk/vim25/{release}`` base via ``_post_vmomi_json``,
    release ``9.0.0.0`` derived from the ``about.version`` above; #2466).
    """
    mock.post("/api/session").respond(200, json=SESSION_TOKEN)
    mock.get("/api/about").respond(200, json=ABOUT_PAYLOAD)
    mock.delete("/api/session").respond(204)
    mock.get("/api/vcenter/host").respond(200, json=HOST_LISTING)
    mock.post("/sdk/vim25/9.0.0.0/PropertyCollector/propertyCollector/RetrievePropertiesEx").mock(
        side_effect=_retrieve_properties_response
    )


# ---------------------------------------------------------------------------
# Target stub
# ---------------------------------------------------------------------------


@dataclass
class _VcsimTarget:
    name: str
    host: str
    port: int | None
    secret_ref: str
    auth_model: str | None = AuthModel.SHARED_SERVICE_ACCOUNT.value
    # Tenant-unique cache key components (#1642/#1672); without them
    # ``target_cache_key`` raises AttributeError at runtime — the exact
    # gap the Harbor integration double had that only the testcontainers
    # lane caught in #1642.
    id: UUID = field(default_factory=uuid4)
    tenant_id: UUID = field(default_factory=lambda: UUID(int=0))


@pytest.fixture
def vcsim_target() -> _VcsimTarget:
    """Target pointing at the respx-mocked vCenter base URL."""
    return _VcsimTarget(
        name="vcsim-test",
        host=VCENTER_BASE_URL.removeprefix("https://"),
        port=443,
        secret_ref="vsphere/vcsim-test",
    )


@pytest.fixture
async def vcsim_connector(
    vcsim_target: _VcsimTarget,
) -> AsyncIterator[tuple[VmwareRestConnector, _VcsimTarget]]:
    """Yield a connector wired against the respx-mocked vCenter surface.

    Only the Vault-backed session loader is replaced (the acceptance
    suite has no Vault); the connector's real ``_http_client`` is left
    intact — respx intercepts httpx at the transport layer, so the
    production pooling + redirect code stays on the exercised path.
    The router stays active across teardown so ``aclose``'s ``DELETE
    /api/session`` is intercepted.
    """

    async def _loader(_target: VsphereTargetLike, _operator: Operator) -> dict[str, str]:
        return {"username": "user", "password": "pass"}

    connector = VmwareRestConnector(session_loader=_loader)

    async with respx.mock(
        base_url=VCENTER_BASE_URL,
        assert_all_called=False,
        assert_all_mocked=False,
    ) as mock:
        _register_vcenter_routes(mock)
        try:
            yield connector, vcsim_target
        finally:
            await connector.aclose()


# ---------------------------------------------------------------------------
# Tests — assertions unchanged from the vcsim-container era; only the
# transport moved (vcsim → respx) because vcsim cannot serve this API.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fingerprint_against_vcsim_returns_reachable(
    vcsim_connector: tuple[VmwareRestConnector, _VcsimTarget],
) -> None:
    """fingerprint() returns reachable=True with the vmware vendor + mapped product."""
    connector, target = vcsim_connector
    result = await connector.fingerprint(target)
    assert result.vendor == "vmware"
    assert result.reachable is True, f"fingerprint not reachable: extras={dict(result.extras)}"
    assert result.probe_method == "GET /api/about"
    # product_line_id="vpx" maps through product_from_line_id -> "vcenter".
    assert result.product == "vcenter"


@pytest.mark.asyncio
async def test_probe_against_vcsim_returns_ok(
    vcsim_connector: tuple[VmwareRestConnector, _VcsimTarget],
) -> None:
    """probe() returns ok=True (delegates to fingerprint)."""
    connector, target = vcsim_connector
    result = await connector.probe(target)
    assert result.ok is True, f"probe failed: reason={result.reason!r}"


@pytest.mark.asyncio
async def test_session_reused_across_consecutive_fingerprint_calls(
    vcsim_connector: tuple[VmwareRestConnector, _VcsimTarget],
) -> None:
    """Two consecutive fingerprint calls share the same cached session token."""
    connector, target = vcsim_connector
    cache_key = target_cache_key(target)
    await connector.fingerprint(target)
    token_after_first = connector._session_tokens.get(cache_key)
    assert token_after_first is not None
    await connector.fingerprint(target)
    token_after_second = connector._session_tokens.get(cache_key)
    # Load-bearing: the cached token is byte-identical across calls
    # (no re-establish).
    assert token_after_first == token_after_second


@pytest.mark.asyncio
async def test_aclose_revokes_session_against_vcsim(
    vcsim_connector: tuple[VmwareRestConnector, _VcsimTarget],
) -> None:
    """aclose() issues DELETE /api/session and clears the token + client caches."""
    connector, target = vcsim_connector

    await connector.fingerprint(target)
    assert target_cache_key(target) in connector._session_tokens

    await connector.aclose()
    # Post-aclose: token cache + client pool both emptied. (The fixture
    # teardown calls aclose() again — idempotent no-op on empty state.)
    assert connector._session_tokens == {}
    assert connector._clients == {}


# ---------------------------------------------------------------------------
# vmware.host.usage typed op (#2257) — end-to-end transport + mount routing
# ---------------------------------------------------------------------------


def _operator() -> Operator:
    """Operator with a non-empty raw_jwt (the session-establish guard)."""
    return Operator(
        sub="op-host-usage-integration",
        name="Host Usage Integration",
        email=None,
        raw_jwt="<integration-raw-jwt>",
        tenant_id=UUID(int=0),
        tenant_role=TenantRole.OPERATOR,
    )


@pytest.mark.asyncio
async def test_host_usage_over_modern_mount_returns_per_host_utilisation(
    vcsim_connector: tuple[VmwareRestConnector, _VcsimTarget],
) -> None:
    """host_usage() runs the full session → list → per-host RetrievePropertiesEx path.

    The modern ``POST /api/session`` succeeds, so both the host listing
    and the PropertyCollector call mount onto ``/api``. Exercises the real
    connector transport (respx-intercepted) end to end — no dispatch_child,
    no ingested descriptor.
    """
    connector, target = vcsim_connector

    result = await connector.host_usage(_operator(), target, {})

    rows = result["hosts"]
    assert [r["id"] for r in rows] == ["host-11", "host-22"]
    assert rows[0]["quick_stats"] == {
        "overall_cpu_usage_mhz": 5200,
        "overall_memory_usage_mb": 131072,
        "uptime_seconds": 1728000,
    }
    assert rows[0]["hardware"]["num_cpu_cores"] == 48
    assert rows[0]["hardware"]["memory_size_bytes"] == 274877906944
    # The maintenance flag is echoed per host by the respx side-effect.
    assert rows[0]["in_maintenance_mode"] is False
    assert rows[1]["in_maintenance_mode"] is True


@pytest.mark.asyncio
async def test_host_usage_over_legacy_mount_routes_to_rest(
    vcsim_target: _VcsimTarget,
) -> None:
    """A legacy-only target (modern /api/session 404s) mounts host.usage onto /rest.

    Reproduces the vcsim / old-vCenter topology: ``POST /api/session``
    404s, the connector falls back to the legacy
    ``POST /rest/com/vmware/cis/session``, and every subsequent op —
    including the typed host.usage listing + PropertyCollector call — must
    route through ``/rest`` (not ``/api``) via ``mount_op_path``, or it
    404s. This is the modern+legacy mount coverage the acceptance criteria
    require.
    """

    async def _loader(_target: VsphereTargetLike, _operator: Operator) -> dict[str, str]:
        return {"username": "user", "password": "pass"}

    connector = VmwareRestConnector(session_loader=_loader)

    async with respx.mock(
        base_url=VCENTER_BASE_URL,
        assert_all_called=False,
        assert_all_mocked=False,
    ) as mock:
        # Modern session endpoint is absent (404) → legacy fallback.
        mock.post("/api/session").respond(404)
        mock.post("/rest/com/vmware/cis/session").respond(200, json=SESSION_TOKEN)
        # Ops are served ONLY under the legacy /rest mount; a request to
        # the /api mount would 404 (unmocked → respx passthrough-less).
        mock.get("/rest/vcenter/host").respond(200, json=HOST_LISTING)
        mock.post("/rest/PropertyCollector/propertyCollector/RetrievePropertiesEx").mock(
            side_effect=_retrieve_properties_response
        )
        mock.delete("/rest/com/vmware/cis/session").respond(204)
        try:
            result = await connector.host_usage(_operator(), vcsim_target, {})
        finally:
            await connector.aclose()

    rows = result["hosts"]
    assert [r["id"] for r in rows] == ["host-11", "host-22"]
    assert rows[0]["hardware"]["cpu_mhz"] == 3000


# ---------------------------------------------------------------------------
# Read composite direct-session dispatch (#2253) — end-to-end transport +
# mount routing with ZERO ingested descriptors (fresh-boot contract)
# ---------------------------------------------------------------------------

#: ``GET /vcenter/datastore`` listing (2-datastore seed, bare modern shape).
_DATASTORE_LISTING: list[dict[str, Any]] = [
    {"datastore": "datastore-11", "name": "ds-a", "type": "VMFS"},
    {"datastore": "datastore-22", "name": "ds-b", "type": "NFS"},
]

#: Per-datastore ``GET /vcenter/datastore/{ds}`` detail bodies.
_DATASTORE_DETAIL: dict[str, dict[str, Any]] = {
    "datastore-11": {"capacity": 1000, "free_space": 400, "type": "VMFS"},
    "datastore-22": {"capacity": 5000, "free_space": 2500, "type": "NFS"},
}


def _register_datastore_composite_routes(
    mock: respx.MockRouter, *, mount: str, reject_prefixed_filter: bool = False
) -> None:
    """Register the datastore-usage composite's sub-op surface under *mount*.

    The composite issues, directly on the session (no ingested descriptor,
    no dispatch_child): the datastore listing, one per-datastore detail
    GET, and one per-datastore VM-placement GET filtered by datastore.
    All are mounted onto ``mount`` (``/api`` modern or ``/rest`` legacy) by
    ``mount_op_path``.

    When *reject_prefixed_filter* is set (modern ``/api``), the VM route
    mimics real vCenter 8.x: it returns HTTP 400 for the legacy
    ``filter.``-prefixed query and 200 only for the bare param name. This
    is the regression guard for #2298 — before the fix the composite sent
    ``filter.datastores`` on every mount and 400'd this leg on real
    vCenter, which the path-only respx route previously masked.
    """
    mock.get(f"{mount}/vcenter/datastore").respond(200, json=_DATASTORE_LISTING)
    for ds_id, detail in _DATASTORE_DETAIL.items():
        mock.get(f"{mount}/vcenter/datastore/{ds_id}").respond(200, json=detail)
    if reject_prefixed_filter:

        def _vm_route(request: httpx.Request) -> httpx.Response:
            # Modern /api addresses the FilterSpec field by its bare name
            # and 400s the legacy ``filter.``-prefixed form.
            if "filter." in request.url.query.decode():
                return httpx.Response(400, json={"messages": ["unknown query parameter"]})
            return httpx.Response(200, json=[{"name": "vm-x"}])

        mock.get(f"{mount}/vcenter/vm").mock(side_effect=_vm_route)
    else:
        # A single VM-placement stub serves every per-datastore query; the
        # composite only counts names, so one VM per datastore is enough.
        mock.get(f"{mount}/vcenter/vm").respond(200, json=[{"name": "vm-x"}])


@pytest.mark.asyncio
async def test_datastore_usage_composite_over_modern_mount(
    vcsim_target: _VcsimTarget,
) -> None:
    """The read composite aggregates directly on the session, mounted onto /api.

    Exercises the full #2253 direct-session path end to end against the
    real connector transport (respx-intercepted): session establish ->
    ``mount_op_path`` -> ``_get_json`` per sub-op -> aggregation. No
    ingested ``endpoint_descriptor`` rows exist here, proving the
    fresh-boot / zero-catalog-ingest contract.
    """
    from meho_backplane.connectors.vmware_rest.composites._read import (
        datastore_usage_composite,
    )

    async def _loader(_target: VsphereTargetLike, _operator: Operator) -> dict[str, str]:
        return {"username": "user", "password": "pass"}

    connector = VmwareRestConnector(session_loader=_loader)

    async with respx.mock(
        base_url=VCENTER_BASE_URL,
        assert_all_called=False,
        assert_all_mocked=False,
    ) as mock:
        mock.post("/api/session").respond(200, json=SESSION_TOKEN)
        _register_datastore_composite_routes(mock, mount="/api", reject_prefixed_filter=True)
        mock.delete("/api/session").respond(204)
        try:
            out = await datastore_usage_composite(
                operator=_operator(),
                target=vcsim_target,
                params={},
                connector=connector,
            )
        finally:
            await connector.aclose()

    rows = out["datastores"]
    assert [r["id"] for r in rows] == ["datastore-11", "datastore-22"]
    assert rows[0]["capacity"] == 1000
    assert rows[0]["free_space"] == 400
    # VM-placement enrichment populated on every row: the bare-param query
    # the fix (#2298) sends is accepted (no 400 -> no enrichment_note skip).
    assert rows[0]["vm_count"] == 1
    assert rows[0]["vm_names"] == ["vm-x"]
    assert "enrichment_note" not in rows[0]
    assert rows[1]["vm_count"] == 1
    assert "enrichment_note" not in rows[1]


@pytest.mark.asyncio
async def test_datastore_usage_composite_over_legacy_mount_routes_to_rest(
    vcsim_target: _VcsimTarget,
) -> None:
    """A legacy-only target (modern /api/session 404s) mounts the composite onto /rest.

    Reproduces the vcsim / old-vCenter topology: the modern session
    endpoint 404s, the connector falls back to the legacy path, and every
    composite sub-op must route through ``/rest`` via ``mount_op_path`` or
    it 404s. This is the modern+legacy mount-fallback coverage the #2253
    acceptance criteria require, on the composite path.
    """
    from meho_backplane.connectors.vmware_rest.composites._read import (
        datastore_usage_composite,
    )

    async def _loader(_target: VsphereTargetLike, _operator: Operator) -> dict[str, str]:
        return {"username": "user", "password": "pass"}

    connector = VmwareRestConnector(session_loader=_loader)

    async with respx.mock(
        base_url=VCENTER_BASE_URL,
        assert_all_called=False,
        assert_all_mocked=False,
    ) as mock:
        # Modern session endpoint absent (404) -> legacy fallback.
        mock.post("/api/session").respond(404)
        mock.post("/rest/com/vmware/cis/session").respond(200, json=SESSION_TOKEN)
        # Sub-ops served ONLY under the legacy /rest mount.
        _register_datastore_composite_routes(mock, mount="/rest")
        mock.delete("/rest/com/vmware/cis/session").respond(204)
        try:
            out = await datastore_usage_composite(
                operator=_operator(),
                target=vcsim_target,
                params={},
                connector=connector,
            )
        finally:
            await connector.aclose()

    rows = out["datastores"]
    assert [r["id"] for r in rows] == ["datastore-11", "datastore-22"]
    assert rows[1]["capacity"] == 5000


# ---------------------------------------------------------------------------
# vmware.host.network_uplinks + vmware.host.vsan_health typed ops (#2258) —
# end-to-end transport + mount routing with ZERO ingested descriptors
# ---------------------------------------------------------------------------

#: Per-host ``config.network.pnic`` + ``config.network.proxySwitch``
#: values the network-uplinks RetrievePropertiesEx side-effect returns.
_HOST_PNICS: list[dict[str, Any]] = [
    {
        "device": "vmnic0",
        "mac": "aa:bb:cc:00:00:00",
        "driver": "ixgbe",
        "linkSpeed": {"speedMb": 10000, "duplex": True},
    },
    {"device": "vmnic1", "mac": "aa:bb:cc:00:00:01", "driver": "ixgbe"},
]
_HOST_PROXY_SWITCHES: list[dict[str, Any]] = [
    {
        "key": "key-vim.host.HostProxySwitch-1",
        "dvsName": "DVS-A",
        "dvsUuid": "50 01 aa bb",
        "pnic": ["key-vim.host.PhysicalNic-vmnic0"],
    }
]


def _network_props_response(request: httpx.Request) -> httpx.Response:
    """Build a RetrieveResult echoing the POSTed host moId with network props."""
    body = json.loads(request.content)
    moid = body["specSet"][0]["objectSet"][0]["obj"]["value"]
    return httpx.Response(
        200,
        json={
            "objects": [
                {
                    "obj": {"type": "HostSystem", "value": moid},
                    "propSet": [
                        {"name": "config.network.pnic", "val": _HOST_PNICS},
                        {"name": "config.network.proxySwitch", "val": _HOST_PROXY_SWITCHES},
                    ],
                }
            ]
        },
    )


#: ``VsanQueryVcClusterHealthSummary`` body the vsan-health route returns.
_VSAN_SUMMARY: dict[str, Any] = {
    "overallHealth": "green",
    "groups": [
        {
            "groupId": "com.vmware.vsan.health.test.network",
            "groupName": "Network",
            "groupHealth": "green",
            "groupTests": [
                {
                    "testId": "com.vmware.vsan.health.test.hostconnectivity",
                    "testName": "vSAN cluster partition",
                    "testHealth": "green",
                    "testShortDescription": "Checks host connectivity.",
                }
            ],
        }
    ],
}


@pytest.mark.asyncio
async def test_host_network_uplinks_over_modern_mount_returns_per_host_pnics(
    vcsim_target: _VcsimTarget,
) -> None:
    """host_network_uplinks() lists hosts then reads pnics per host, mounted onto /api.

    Exercises the full #2258 typed-op path end to end against the real
    connector transport (respx-intercepted) with ZERO ingested
    descriptors — the fresh-boot / zero-catalog-ingest contract.
    """

    async def _loader(_target: VsphereTargetLike, _operator: Operator) -> dict[str, str]:
        return {"username": "user", "password": "pass"}

    connector = VmwareRestConnector(session_loader=_loader)

    async with respx.mock(
        base_url=VCENTER_BASE_URL,
        assert_all_called=False,
        assert_all_mocked=False,
    ) as mock:
        mock.post("/api/session").respond(200, json=SESSION_TOKEN)
        # The vmomi read derives its VI-JSON {release} from about.version (#2466).
        mock.get("/api/about").respond(200, json=ABOUT_PAYLOAD)
        mock.get("/api/vcenter/host").respond(200, json=HOST_LISTING)
        mock.post(
            "/sdk/vim25/9.0.0.0/PropertyCollector/propertyCollector/RetrievePropertiesEx"
        ).mock(side_effect=_network_props_response)
        mock.delete("/api/session").respond(204)
        try:
            result = await connector.host_network_uplinks(_operator(), vcsim_target, {})
        finally:
            await connector.aclose()

    rows = result["hosts"]
    assert [r["id"] for r in rows] == ["host-11", "host-22"]
    assert rows[0]["pnics"][0] == {
        "device": "vmnic0",
        "mac": "aa:bb:cc:00:00:00",
        "driver": "ixgbe",
        "link_up": True,
        "speed_mb": 10000,
        "duplex": True,
    }
    assert rows[0]["pnics"][1]["link_up"] is False
    assert rows[0]["proxy_switches"][0]["uplink_pnics"] == ["vmnic0"]
    assert "read_note" not in rows[0]


@pytest.mark.asyncio
async def test_host_vsan_health_over_legacy_mount_routes_to_rest(
    vcsim_target: _VcsimTarget,
) -> None:
    """A legacy-only target mounts the vsan-health query onto /rest.

    The modern ``POST /api/session`` 404s, the connector falls back to the
    legacy path, and the ``VsanQueryVcClusterHealthSummary`` call must
    route through ``/rest`` via ``mount_op_path`` or it 404s — the
    modern+legacy mount coverage on the #2258 typed-op path, with zero
    ingested descriptors.
    """

    async def _loader(_target: VsphereTargetLike, _operator: Operator) -> dict[str, str]:
        return {"username": "user", "password": "pass"}

    connector = VmwareRestConnector(session_loader=_loader)
    vsan_path = (
        "/rest/VsanVcClusterHealthSystem/vsan-cluster-health-system/VsanQueryVcClusterHealthSummary"
    )

    async with respx.mock(
        base_url=VCENTER_BASE_URL,
        assert_all_called=False,
        assert_all_mocked=False,
    ) as mock:
        mock.post("/api/session").respond(404)
        mock.post("/rest/com/vmware/cis/session").respond(200, json=SESSION_TOKEN)
        mock.post(vsan_path).respond(200, json=_VSAN_SUMMARY)
        mock.delete("/rest/com/vmware/cis/session").respond(204)
        try:
            result = await connector.host_vsan_health(
                _operator(), vcsim_target, {"cluster": "domain-c123"}
            )
        finally:
            await connector.aclose()

    assert result["cluster"] == "domain-c123"
    assert result["overall_health"] == "green"
    assert result["groups"][0]["group_id"] == "com.vmware.vsan.health.test.network"
    assert result["groups"][0]["tests"][0]["test_health"] == "green"
    assert "read_note" not in result


# ---------------------------------------------------------------------------
# Write composite direct-session dispatch (#2256) — per-op transport + mount
# parity with ZERO ingested descriptors and the real governance seam
# ---------------------------------------------------------------------------
#
# These exercise the migrated *write* composites end to end against the real
# connector transport (respx-intercepted): session establish -> mount_op_path
# -> enforce_subop_policy (auto-executes for the USER operator) -> the direct
# _post_json write. No ingested endpoint_descriptor rows exist, proving the
# fresh-boot contract on the write path (Task #2256, Initiative #2249).


def _write_operator() -> Operator:
    """USER operator — enforce_subop_policy auto-executes its dangerous writes.

    The per-sub-op governance seam runs for real here; a human/service
    principal on a ``requires_approval=False`` sub-op clears the gate
    synchronously (no DB), so the write proceeds and the parity assertion
    sees the real wire request.
    """
    return Operator(
        sub="op-write-composite-parity",
        name="Write Composite Parity",
        email=None,
        raw_jwt="<integration-raw-jwt>",
        tenant_id=UUID(int=0),
        tenant_role=TenantRole.OPERATOR,
    )


@pytest.mark.asyncio
async def test_vm_create_composite_over_modern_mount(
    vcsim_target: _VcsimTarget,
) -> None:
    """vm.create runs its folder read + create/NIC/power writes on the /api session.

    Full #2256 direct-session write path end to end against the real connector
    transport, with the real ``enforce_subop_policy`` seam clearing each write
    for the USER operator. Zero ingested descriptors.
    """
    from meho_backplane.connectors.vmware_rest.composites._write import vm_create_composite

    async def _loader(_target: VsphereTargetLike, _operator: Operator) -> dict[str, str]:
        return {"username": "user", "password": "pass"}

    connector = VmwareRestConnector(session_loader=_loader)

    async with respx.mock(
        base_url=VCENTER_BASE_URL,
        assert_all_called=False,
        assert_all_mocked=False,
    ) as mock:
        mock.post("/api/session").respond(200, json=SESSION_TOKEN)
        mock.get("/api/vcenter/folder").respond(200, json=[{"folder": "group-1", "name": "prod"}])
        create_route = mock.post("/api/vcenter/vm").respond(200, json="vm-1")
        nic_route = mock.patch("/api/vcenter/vm/vm-1/network").respond(200, json={})
        power_route = mock.post("/api/vcenter/vm/vm-1/power").respond(200, json={})
        mock.delete("/api/session").respond(204)
        try:
            out = await vm_create_composite(
                operator=_write_operator(),
                target=vcsim_target,
                params={
                    "folder_name": "prod",
                    "name": "web-01",
                    "guest_os": "UBUNTU_64",
                    "nics": [{"network": "net-1"}],
                    "power_on_after_create": True,
                },
                connector=connector,
            )
        finally:
            await connector.aclose()

    assert out["status"] == "created"
    assert out["vm_id"] == "vm-1"
    assert out["steps_succeeded"] == ["folder_lookup", "create", "nic_attach", "power_on"]
    # The create/NIC/power writes reached the wire on the modern /api mount.
    assert create_route.called
    assert nic_route.called
    assert power_route.called
    # The create body carries the spec wrapper the vCenter POST expects.
    create_body = json.loads(create_route.calls.last.request.content)
    assert create_body["spec"]["name"] == "web-01"
    assert create_body["spec"]["placement"]["folder"] == "group-1"


@pytest.mark.asyncio
async def test_cluster_patch_composite_over_legacy_mount_routes_to_rest(
    vcsim_target: _VcsimTarget,
) -> None:
    """A legacy-only target mounts cluster.patch's writes onto /rest.

    The modern ``POST /api/session`` 404s, the connector falls back to the
    legacy path, and every write sub-op (maintenance enter/exit PATCH, host
    patch POST) must route through ``/rest`` via ``mount_op_path`` or it 404s
    — the modern+legacy mount coverage on the #2256 write path.
    """
    from meho_backplane.connectors.vmware_rest.composites._write import cluster_patch_composite

    async def _loader(_target: VsphereTargetLike, _operator: Operator) -> dict[str, str]:
        return {"username": "user", "password": "pass"}

    connector = VmwareRestConnector(session_loader=_loader)

    async with respx.mock(
        base_url=VCENTER_BASE_URL,
        assert_all_called=False,
        assert_all_mocked=False,
    ) as mock:
        mock.post("/api/session").respond(404)
        mock.post("/rest/com/vmware/cis/session").respond(200, json=SESSION_TOKEN)
        mock.get("/rest/vcenter/cluster/domain-c1/host").respond(200, json=[{"host": "host-1"}])
        enter_route = mock.patch("/rest/vcenter/host/host-1/maintenance").respond(200, json={})
        patch_route = mock.post("/rest/vcenter/host/host-1").respond(200, json={})
        mock.delete("/rest/com/vmware/cis/session").respond(204)
        try:
            out = await cluster_patch_composite(
                operator=_write_operator(),
                target=vcsim_target,
                params={"cluster": "domain-c1"},
                connector=connector,
            )
        finally:
            await connector.aclose()

    assert out["status"] == "completed"
    assert out["patched_hosts"] == ["host-1"]
    # maintenance enter + exit both hit the same PATCH route; the patch POST hit /rest.
    assert enter_route.call_count == 2
    assert patch_route.called


@pytest.mark.asyncio
async def test_host_evacuate_recursion_over_modern_mount(
    vcsim_target: _VcsimTarget,
) -> None:
    """host.evacuate's vm.migrate recursion runs its relocate write on the session.

    Parity for the composite->composite recursion (#2248 carve-out): the
    ``dispatch_child`` call into ``vmware.composite.vm.migrate`` resolves the
    same connector and runs the inner DRS read + relocate write on the direct
    /api session, and host.evacuate then enters maintenance — all against the
    real transport, zero ingested descriptors.
    """
    from meho_backplane.connectors.vmware_rest.composites._write import (
        host_evacuate_composite,
        vm_migrate_composite,
    )

    async def _loader(_target: VsphereTargetLike, _operator: Operator) -> dict[str, str]:
        return {"username": "user", "password": "pass"}

    connector = VmwareRestConnector(session_loader=_loader)
    operator = _write_operator()

    async def _recursive_dispatch_child(
        *, connector_id: str, op_id: str, params: dict[str, Any], target: Any = None
    ) -> Any:
        # Mirror the dispatcher's composite->composite recursion: resolve
        # vm.migrate and run it on the same connector session.
        from meho_backplane.connectors import OperationResult

        assert op_id == "vmware.composite.vm.migrate"
        inner = await vm_migrate_composite(
            operator=operator, target=vcsim_target, params=params, connector=connector
        )
        return OperationResult(status="ok", op_id=op_id, result=inner, duration_ms=1.0)

    async with respx.mock(
        base_url=VCENTER_BASE_URL,
        assert_all_called=False,
        assert_all_mocked=False,
    ) as mock:
        mock.post("/api/session").respond(200, json=SESSION_TOKEN)
        mock.get("/api/vcenter/vm").respond(200, json=[{"vm": "vm-a", "cluster": "domain-c1"}])
        mock.get("/api/vcenter/cluster/domain-c1/drs/recommendations").respond(
            200, json=[{"vm": "vm-a", "target_host": "host-target"}]
        )
        relocate_route = mock.post("/api/vcenter/vm/vm-a").respond(200, json={})
        maintenance_route = mock.patch("/api/vcenter/host/host-1/maintenance").respond(200, json={})
        mock.delete("/api/session").respond(204)
        try:
            out = await host_evacuate_composite(
                operator=operator,
                target=vcsim_target,
                params={"host": "host-1"},
                connector=connector,
                dispatch_child=_recursive_dispatch_child,
            )
        finally:
            await connector.aclose()

    assert out["status"] == "evacuated"
    assert out["migrated_vms"] == ["vm-a"]
    assert out["maintenance_entered"] is True
    # The recursion's relocate write and the host maintenance-enter write both
    # reached the /api session.
    assert relocate_route.called
    assert maintenance_route.called
    relocate_body = json.loads(relocate_route.calls.last.request.content)
    assert relocate_body["spec"]["placement"]["host"] == "host-target"


# ---------------------------------------------------------------------------
# vmware.vm.info + vmware.object.collect + vmware.tasks.recent typed ops
# (#2300) — end-to-end transport + mount routing with ZERO ingested descriptors
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_vm_info_over_modern_mount_returns_guest_signals(
    vcsim_target: _VcsimTarget,
) -> None:
    """vm_info() by name resolves the moid then reads guest/runtime/storage props.

    The name resolution GET and the PropertyCollector read both mount onto
    ``/api``. Exercises the full #2300 typed-op path end to end against the
    real connector transport (respx-intercepted) with ZERO ingested
    descriptors — the fresh-boot / zero-catalog-ingest contract.
    """

    async def _loader(_target: VsphereTargetLike, _operator: Operator) -> dict[str, str]:
        return {"username": "user", "password": "pass"}

    connector = VmwareRestConnector(session_loader=_loader)

    def _vm_props(request: httpx.Request) -> httpx.Response:
        moid = json.loads(request.content)["specSet"][0]["objectSet"][0]["obj"]["value"]
        return httpx.Response(
            200,
            json={
                "objects": [
                    {
                        "obj": {"type": "VirtualMachine", "value": moid},
                        "propSet": [
                            {"name": "name", "val": "hung-appliance"},
                            {"name": "runtime.powerState", "val": "poweredOn"},
                            {"name": "guestHeartbeatStatus", "val": "red"},
                            {
                                "name": "storage.perDatastoreUsage",
                                "val": [
                                    {
                                        "datastore": {"type": "Datastore", "value": "datastore-9"},
                                        "committed": 42949672960,
                                    }
                                ],
                            },
                        ],
                    }
                ]
            },
        )

    async with respx.mock(
        base_url=VCENTER_BASE_URL,
        assert_all_called=False,
        assert_all_mocked=False,
    ) as mock:
        mock.post("/api/session").respond(200, json=SESSION_TOKEN)
        # The vmomi read derives its VI-JSON {release} from about.version (#2466).
        mock.get("/api/about").respond(200, json=ABOUT_PAYLOAD)
        mock.get("/api/vcenter/vm").respond(200, json=[{"vm": "vm-9", "name": "hung-appliance"}])
        mock.post(
            "/sdk/vim25/9.0.0.0/PropertyCollector/propertyCollector/RetrievePropertiesEx"
        ).mock(side_effect=_vm_props)
        mock.delete("/api/session").respond(204)
        try:
            result = await connector.vm_info(_operator(), vcsim_target, {"name": "hung-appliance"})
        finally:
            await connector.aclose()

    assert result["vm"] == "vm-9"
    # The hung-appliance shape: poweredOn but no guest IP, red heartbeat.
    assert result["power_state"] == "poweredOn"
    assert result["guest_ip"] is None
    assert result["heartbeat_status"] == "red"
    assert result["per_datastore_usage"][0]["datastore"] == "datastore-9"
    assert result["per_datastore_usage"][0]["committed_bytes"] == 42949672960


@pytest.mark.asyncio
async def test_object_collect_over_legacy_mount_routes_to_rest(
    vcsim_target: _VcsimTarget,
) -> None:
    """A legacy-only target mounts the bounded generic read onto /rest.

    The modern ``POST /api/session`` 404s, the connector falls back to the
    legacy path, and the RetrievePropertiesEx call must route through
    ``/rest`` via ``mount_op_path`` or it 404s — modern+legacy mount
    coverage on the #2300 typed-op path, with zero ingested descriptors.
    """

    async def _loader(_target: VsphereTargetLike, _operator: Operator) -> dict[str, str]:
        return {"username": "user", "password": "pass"}

    connector = VmwareRestConnector(session_loader=_loader)
    props_path = "/rest/PropertyCollector/propertyCollector/RetrievePropertiesEx"

    async with respx.mock(
        base_url=VCENTER_BASE_URL,
        assert_all_called=False,
        assert_all_mocked=False,
    ) as mock:
        mock.post("/api/session").respond(404)
        mock.post("/rest/com/vmware/cis/session").respond(200, json=SESSION_TOKEN)
        mock.post(props_path).respond(
            200,
            json={
                "objects": [
                    {
                        "obj": {"type": "Datastore", "value": "datastore-5"},
                        "propSet": [{"name": "summary.freeSpace", "val": 1073741824}],
                        "missingSet": [{"path": "summary.capacity"}],
                    }
                ]
            },
        )
        mock.delete("/rest/com/vmware/cis/session").respond(204)
        try:
            result = await connector.object_collect(
                _operator(),
                vcsim_target,
                {
                    "type": "Datastore",
                    "moid": "datastore-5",
                    "properties": ["summary.freeSpace", "summary.capacity"],
                },
            )
        finally:
            await connector.aclose()

    assert result["type"] == "Datastore"
    assert result["properties"]["summary.freeSpace"] == 1073741824
    assert result["missing"] == ["summary.capacity"]


@pytest.mark.asyncio
async def test_tasks_recent_over_modern_mount_returns_task_rows(
    vcsim_target: _VcsimTarget,
) -> None:
    """tasks_recent() reads TaskManager.recentTask then Task.info, mounted onto /api."""

    async def _loader(_target: VsphereTargetLike, _operator: Operator) -> dict[str, str]:
        return {"username": "user", "password": "pass"}

    connector = VmwareRestConnector(session_loader=_loader)

    def _tasks(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        mo_type = body["specSet"][0]["propSet"][0]["type"]
        if mo_type == "TaskManager":
            return httpx.Response(
                200,
                json={
                    "objects": [
                        {
                            "obj": {"type": "TaskManager", "value": "TaskManager"},
                            "propSet": [
                                {
                                    "name": "recentTask",
                                    "val": [{"type": "Task", "value": "task-1"}],
                                }
                            ],
                        }
                    ]
                },
            )
        return httpx.Response(
            200,
            json={
                "objects": [
                    {
                        "obj": {"type": "Task", "value": "task-1"},
                        "propSet": [
                            {
                                "name": "info",
                                "val": {
                                    "descriptionId": "VirtualMachine.powerOn",
                                    "entity": {"type": "VirtualMachine", "value": "vm-9"},
                                    "entityName": "web-01",
                                    "state": "success",
                                    "progress": 100,
                                },
                            }
                        ],
                    }
                ]
            },
        )

    async with respx.mock(
        base_url=VCENTER_BASE_URL,
        assert_all_called=False,
        assert_all_mocked=False,
    ) as mock:
        mock.post("/api/session").respond(200, json=SESSION_TOKEN)
        # The vmomi read derives its VI-JSON {release} from about.version (#2466).
        mock.get("/api/about").respond(200, json=ABOUT_PAYLOAD)
        mock.post(
            "/sdk/vim25/9.0.0.0/PropertyCollector/propertyCollector/RetrievePropertiesEx"
        ).mock(side_effect=_tasks)
        mock.delete("/api/session").respond(204)
        try:
            result = await connector.tasks_recent(_operator(), vcsim_target, {"max_tasks": 10})
        finally:
            await connector.aclose()

    (task,) = result["tasks"]
    assert task["task"] == "task-1"
    assert task["operation"] == "VirtualMachine.powerOn"
    assert task["entity"] == "vm-9"
    assert task["state"] == "success"
    assert task["progress"] == 100
