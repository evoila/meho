# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Unit tests for the VI-JSON vmomi mount seam (#2466).

vmomi (VI-JSON) methods — ``RetrievePropertiesEx``,
``VsanQueryVcClusterHealthSummary``, ``QueryEvents``, ``QueryPerf`` … —
are served by vCenter under the documented release-versioned base
``/sdk/vim25/{release}/{MoType}/{moId}/{method}`` (Broadcom Web Services
SDK guide, "Building JSON Request URLs"), available since vCenter 8.0U1.
Mounting them under the vSphere Automation ``/api`` mount 404s on vCenter
8.0.x; the ``/api`` form works only on the 9.0.2 fleet and is kept as a
single fallback.

These tests cover:

* the pure ``{release}`` derivation + path build in ``._mount``;
* :meth:`VmwareRestConnector._post_vmomi_json` against a real httpx
  transport (respx): the ``/sdk/vim25/{release}`` URL construction, the
  single ``/api`` 404 fallback, the both-404 diagnostic error, the legacy
  ``/rest`` passthrough (no VI-JSON on vcsim), and the ``about``-version
  caching.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
import respx

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors.schemas import AuthModel
from meho_backplane.connectors.vmware_rest import VmwareRestConnector, VsphereTargetLike
from meho_backplane.connectors.vmware_rest._mount import (
    vmomi_mounted_path,
    vmomi_release_from_version,
)

_RETRIEVE_PROPERTIES_PATH = "/PropertyCollector/propertyCollector/RetrievePropertiesEx"
_RETRIEVE_BODY: dict[str, Any] = {"specSet": [], "options": {}}


# ---------------------------------------------------------------------------
# Pure helpers: release derivation + path build
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("version", "expected"),
    [
        # Three-part about.version pads to the four-part VI-JSON release.
        ("8.0.3", "8.0.3.0"),
        ("8.0.2", "8.0.2.0"),
        ("8.0", "8.0.0.0"),
        # Already four-part passes through (the 9.x fleet).
        ("9.0.0.0", "9.0.0.0"),
        ("9.0.2.1", "9.0.2.1"),
        # No usable numeric prefix -> None (caller falls back to /api).
        ("", None),
        (None, None),
        ("garbage", None),
    ],
)
def test_vmomi_release_from_version(version: str | None, expected: str | None) -> None:
    assert vmomi_release_from_version(version) == expected


def test_vmomi_mounted_path_prefixes_the_documented_vi_json_base() -> None:
    assert (
        vmomi_mounted_path("8.0.3.0", _RETRIEVE_PROPERTIES_PATH)
        == "/sdk/vim25/8.0.3.0/PropertyCollector/propertyCollector/RetrievePropertiesEx"
    )


def test_vmomi_mounted_path_normalises_a_missing_leading_slash() -> None:
    assert (
        vmomi_mounted_path("9.0.0.0", "Task/task-1/Cancel")
        == "/sdk/vim25/9.0.0.0/Task/task-1/Cancel"
    )


# ---------------------------------------------------------------------------
# Connector transport: _post_vmomi_json against a real httpx (respx) client
# ---------------------------------------------------------------------------


@dataclass
class _StubTarget:
    name: str = "vc-8"
    host: str = "vc-8.test.invalid"
    port: int | None = 443
    secret_ref: str = "vsphere/vc-8"
    auth_model: str | None = AuthModel.SHARED_SERVICE_ACCOUNT.value
    id: UUID = field(default_factory=uuid4)
    tenant_id: UUID = field(default_factory=lambda: UUID(int=0))


def _make_operator() -> Operator:
    return Operator(
        sub="op-vmomi-mount",
        name="Vmomi Mount Test",
        email=None,
        raw_jwt="<test-raw-jwt>",
        tenant_id=UUID("00000000-0000-0000-0000-00000000a0a0"),
        tenant_role=TenantRole.OPERATOR,
    )


async def _stub_loader(_target: VsphereTargetLike, _operator: Operator) -> dict[str, str]:
    return {"username": "svc-meho", "password": "stub-password"}


def _make_connector() -> VmwareRestConnector:
    return VmwareRestConnector(session_loader=_stub_loader)


def _patch_no_revoke_aclose(connector: VmwareRestConnector) -> None:
    """Skip the session-revoke DELETE at teardown (mirrors the fingerprint tests)."""

    async def _aclose() -> None:
        connector._session_tokens.clear()
        for client in connector._clients.values():
            await client.aclose()
        connector._clients.clear()

    connector.aclose = _aclose  # type: ignore[method-assign]


_BASE = "https://vc-8.test.invalid"
_VIJSON_URL = "/sdk/vim25/8.0.3.0/PropertyCollector/propertyCollector/RetrievePropertiesEx"
_API_URL = "/api/PropertyCollector/propertyCollector/RetrievePropertiesEx"


@pytest.mark.asyncio
async def test_modern_vmomi_read_mounts_on_sdk_vim25_release() -> None:
    """about-version 8.0.3 -> the vmomi POST lands on /sdk/vim25/8.0.3.0/..."""
    connector = _make_connector()
    _patch_no_revoke_aclose(connector)
    try:
        async with respx.mock(base_url=_BASE) as mock:
            mock.post("/api/session").respond(200, json="tok")
            mock.get("/api/about").respond(200, json={"version": "8.0.3"})
            vijson = mock.post(_VIJSON_URL).respond(200, json={"objects": []})
            result = await connector._post_vmomi_json(
                _StubTarget(),
                _RETRIEVE_PROPERTIES_PATH,
                operator=_make_operator(),
                json=_RETRIEVE_BODY,
            )
        assert vijson.called
        assert vijson.call_count == 1
        assert result == {"objects": []}
    finally:
        await connector.aclose()


@pytest.mark.asyncio
async def test_modern_vmomi_read_falls_back_once_to_api_on_404() -> None:
    """A 404 on the /sdk/vim25 mount triggers exactly one /api fallback (AC #4)."""
    connector = _make_connector()
    _patch_no_revoke_aclose(connector)
    try:
        async with respx.mock(base_url=_BASE) as mock:
            mock.post("/api/session").respond(200, json="tok")
            mock.get("/api/about").respond(200, json={"version": "8.0.3"})
            vijson = mock.post(_VIJSON_URL).respond(404, json={})
            api = mock.post(_API_URL).respond(200, json={"objects": ["fallback"]})
            result = await connector._post_vmomi_json(
                _StubTarget(),
                _RETRIEVE_PROPERTIES_PATH,
                operator=_make_operator(),
                json=_RETRIEVE_BODY,
            )
        # Exactly one attempt on each mount: vi-json first, then the single
        # /api fallback.
        assert vijson.call_count == 1
        assert api.call_count == 1
        assert result == {"objects": ["fallback"]}
    finally:
        await connector.aclose()


@pytest.mark.asyncio
async def test_both_mounts_404_raises_diagnostic_naming_both_and_version() -> None:
    """When both mounts 404 the error names both URLs + the vCenter version."""
    connector = _make_connector()
    _patch_no_revoke_aclose(connector)
    try:
        async with respx.mock(base_url=_BASE) as mock:
            mock.post("/api/session").respond(200, json="tok")
            mock.get("/api/about").respond(200, json={"version": "8.0.3"})
            mock.post(_VIJSON_URL).respond(404, json={})
            mock.post(_API_URL).respond(404, json={})
            with pytest.raises(RuntimeError) as excinfo:
                await connector._post_vmomi_json(
                    _StubTarget(),
                    _RETRIEVE_PROPERTIES_PATH,
                    operator=_make_operator(),
                    json=_RETRIEVE_BODY,
                )
        message = str(excinfo.value)
        assert "vi-json unavailable" in message
        assert "/sdk/vim25/8.0.3.0/" in message
        assert "/api/PropertyCollector/" in message
        assert "8.0.3" in message
    finally:
        await connector.aclose()


@pytest.mark.asyncio
async def test_non_404_on_vijson_propagates_without_fallback() -> None:
    """A 500 on the /sdk/vim25 mount is not a 'mount not served' signal -- propagate."""
    connector = _make_connector()
    _patch_no_revoke_aclose(connector)
    try:
        async with respx.mock(base_url=_BASE, assert_all_called=False) as mock:
            mock.post("/api/session").respond(200, json="tok")
            mock.get("/api/about").respond(200, json={"version": "8.0.3"})
            mock.post(_VIJSON_URL).respond(500, json={})
            api = mock.post(_API_URL).respond(200, json={"objects": []})
            with pytest.raises(httpx.HTTPStatusError):
                await connector._post_vmomi_json(
                    _StubTarget(),
                    _RETRIEVE_PROPERTIES_PATH,
                    operator=_make_operator(),
                    json=_RETRIEVE_BODY,
                )
        # No fallback on a 5xx.
        assert not api.called
    finally:
        await connector.aclose()


@pytest.mark.asyncio
async def test_legacy_session_uses_rest_mount_and_skips_vi_json() -> None:
    """A vcsim/legacy target (session on /rest) reaches vmomi via /rest, no VI-JSON."""
    connector = _make_connector()
    _patch_no_revoke_aclose(connector)
    try:
        async with respx.mock(base_url=_BASE, assert_all_called=False) as mock:
            # Modern session endpoint 404s -> legacy fallback establishes.
            mock.post("/api/session").respond(404, json={})
            mock.post("/rest/com/vmware/cis/session").respond(200, json={"value": "tok"})
            about = mock.get("/api/about").respond(200, json={"version": "8.0.3"})
            vijson = mock.post(_VIJSON_URL).respond(200, json={})
            rest = mock.post(
                "/rest/PropertyCollector/propertyCollector/RetrievePropertiesEx"
            ).respond(200, json={"objects": ["rest"]})
            result = await connector._post_vmomi_json(
                _StubTarget(),
                _RETRIEVE_PROPERTIES_PATH,
                operator=_make_operator(),
                json=_RETRIEVE_BODY,
            )
        assert result == {"objects": ["rest"]}
        assert rest.call_count == 1
        # Legacy path never probes about-version and never attempts VI-JSON.
        assert not about.called
        assert not vijson.called
    finally:
        await connector.aclose()


@pytest.mark.asyncio
async def test_unresolvable_about_version_goes_straight_to_api() -> None:
    """When /api/about can't answer, the vmomi read uses the /api mount directly."""
    connector = _make_connector()
    _patch_no_revoke_aclose(connector)
    try:
        async with respx.mock(base_url=_BASE, assert_all_called=False) as mock:
            mock.post("/api/session").respond(200, json="tok")
            mock.get("/api/about").respond(500, json={})
            vijson = mock.post(_VIJSON_URL).respond(200, json={})
            api = mock.post(_API_URL).respond(200, json={"objects": ["api"]})
            result = await connector._post_vmomi_json(
                _StubTarget(),
                _RETRIEVE_PROPERTIES_PATH,
                operator=_make_operator(),
                json=_RETRIEVE_BODY,
            )
        assert result == {"objects": ["api"]}
        assert api.call_count == 1
        assert not vijson.called
    finally:
        await connector.aclose()


@pytest.mark.asyncio
async def test_about_version_resolved_once_and_cached_across_reads() -> None:
    """The about-version probe runs once per target and is reused (AC caching)."""
    connector = _make_connector()
    _patch_no_revoke_aclose(connector)
    target = _StubTarget()
    try:
        async with respx.mock(base_url=_BASE) as mock:
            mock.post("/api/session").respond(200, json="tok")
            about = mock.get("/api/about").respond(200, json={"version": "8.0.3"})
            mock.post(_VIJSON_URL).respond(200, json={"objects": []})
            await connector._post_vmomi_json(
                target, _RETRIEVE_PROPERTIES_PATH, operator=_make_operator(), json=_RETRIEVE_BODY
            )
            await connector._post_vmomi_json(
                target, _RETRIEVE_PROPERTIES_PATH, operator=_make_operator(), json=_RETRIEVE_BODY
            )
        # Two vmomi reads, one /api/about probe.
        assert about.call_count == 1
    finally:
        await connector.aclose()
