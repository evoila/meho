# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Unit tests for :class:`VcfFleetConnector` auth + fingerprint/probe (G3.6-T7 #831).

Exercises HTTP Basic auth against Fleet's LCM-local user store (typical
``admin@local``; no SSO federation), per-target credential isolation,
the auth_model boundary gate, and the fingerprint/probe shapes against
the wrapper-verified probe call.

The fingerprint path mirrors the consumer wrapper
``scripts/vcf-fleet.sh``: ``GET /lcm/lcops/api/v2/datacenters`` with
HTTP Basic auth, reading the ``Lcm-API-Version`` response header for
the LCM API version. Fleet's first-party diagnostic endpoints
(``/about``, ``/health``, ``/version``, ``/system-details``) return
HTTP 500 in VCF 9.0 builds — the wrapper documents this and the
connector follows the wrapper's workaround verbatim.

Test layout mirrors :mod:`tests.test_connectors_harbor_auth` (HTTP
Basic + Vault-loader-via-injectable + fingerprint/probe shape) and the
fixture-clean pattern :mod:`tests.test_connectors_vcf_automation_auth`
established.
"""

from __future__ import annotations

import base64
from collections.abc import Iterator
from dataclasses import dataclass

import httpx
import pytest
import respx

from meho_backplane.connectors.adapters.http import HttpConnector
from meho_backplane.connectors.registry import (
    clear_registry,
    register_connector_v2,
)
from meho_backplane.connectors.schemas import AuthModel
from meho_backplane.connectors.vcf_fleet import (
    VcfFleetConnector,
    VcfFleetTargetLike,
)


@pytest.fixture(autouse=True)
def _clean_vcf_fleet_registry() -> Iterator[None]:
    """Re-register VcfFleetConnector after sibling tests clear the registry.

    ``test_connectors_registry_v2.py`` installs an autouse fixture that
    calls :func:`clear_registry` between tests. Re-register before
    every test in this module and clear after — same pattern
    :mod:`tests.test_connectors_harbor_auth` /
    :mod:`tests.test_connectors_vcf_automation_auth` established.
    """
    clear_registry()
    register_connector_v2(
        product=VcfFleetConnector.product,
        version=VcfFleetConnector.version,
        impl_id=VcfFleetConnector.impl_id,
        cls=VcfFleetConnector,
    )
    yield
    clear_registry()


# ---------------------------------------------------------------------------
# Target stub — satisfies VcfFleetTargetLike Protocol structurally.
# Replaced by the real Target model when G0.3 (#224) is fully wired in.
# ---------------------------------------------------------------------------


@dataclass
class _StubTarget:
    name: str
    host: str
    port: int | None
    secret_ref: str
    auth_model: str | None = AuthModel.SHARED_SERVICE_ACCOUNT.value


_TARGET_A = _StubTarget(
    name="vcf-fleet-a",
    host="vcf-fleet-a.test.invalid",
    port=443,
    secret_ref="kv/data/vcf-fleet/vcf-fleet-a",
)
_TARGET_B = _StubTarget(
    name="vcf-fleet-b",
    host="vcf-fleet-b.test.invalid",
    port=443,
    secret_ref="kv/data/vcf-fleet/vcf-fleet-b",
)


async def _stub_loader(_target: VcfFleetTargetLike) -> dict[str, str]:
    """Return canned ``admin@local`` credentials regardless of the target."""
    return {"username": "admin@local", "password": "stub-password"}


def _make_connector() -> VcfFleetConnector:
    """Build a connector wired with the stub loader."""
    return VcfFleetConnector(credentials_loader=_stub_loader)


def _decode_basic_auth(authorization_header: str) -> tuple[str, str]:
    """Decode an ``Authorization: Basic <b64>`` header into (username, password)."""
    assert authorization_header.startswith("Basic ")
    decoded = base64.b64decode(authorization_header[6:]).decode()
    username, _, password = decoded.partition(":")
    return username, password


# ---------------------------------------------------------------------------
# ABC + registration plumbing
# ---------------------------------------------------------------------------


def test_vcf_fleet_connector_subclasses_http_connector() -> None:
    """Sanity check: the connector inherits from HttpConnector with the right metadata."""
    assert issubclass(VcfFleetConnector, HttpConnector)
    assert VcfFleetConnector.product == "vcf-fleet"
    assert VcfFleetConnector.version == "9.0"
    assert VcfFleetConnector.impl_id == "fleet-rest"
    assert VcfFleetConnector.supported_version_range == ">=9.0,<10.0"
    assert VcfFleetConnector.priority == 1


def test_importing_package_registers_against_v2_registry() -> None:
    """The package's __init__ calls register_connector_v2 at import time."""
    from meho_backplane.connectors.registry import all_connectors_v2

    registry = all_connectors_v2()
    key = ("vcf-fleet", "9.0", "fleet-rest")
    assert key in registry
    assert registry[key] is VcfFleetConnector


def test_default_credentials_loader_raises_until_goal_214() -> None:
    """The default Vault loader stays unimplemented until Goal #214."""
    import asyncio

    from meho_backplane.connectors.vcf_fleet.session import (
        load_credentials_from_vault,
    )

    async def _check() -> None:
        with pytest.raises(NotImplementedError, match=r"Goal #214"):
            await load_credentials_from_vault(_TARGET_A)

    asyncio.run(_check())


# ---------------------------------------------------------------------------
# HTTP Basic auth — admin@local form
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_auth_headers_sends_basic_auth_for_admin_at_local() -> None:
    """auth_headers() produces Authorization: Basic with the literal admin@local username."""
    connector = _make_connector()
    headers = await connector.auth_headers(_TARGET_A, raw_jwt="")

    assert "Authorization" in headers
    assert headers["Authorization"].startswith("Basic ")
    username, password = _decode_basic_auth(headers["Authorization"])
    # admin@local is sent verbatim — the @local suffix is part of the
    # username, not a realm decoration. Fleet does NOT federate SSO.
    assert username == "admin@local"
    assert password == "stub-password"
    await connector.aclose()


# ---------------------------------------------------------------------------
# Credential caching — load once per target
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_auth_headers_reuses_cached_credentials_across_calls() -> None:
    """Second auth_headers call against the same target does NOT re-invoke the loader."""
    call_count = 0

    async def _counting_loader(_target: VcfFleetTargetLike) -> dict[str, str]:
        nonlocal call_count
        call_count += 1
        return {"username": "admin@local", "password": "stub-password"}

    connector = VcfFleetConnector(credentials_loader=_counting_loader)
    h1 = await connector.auth_headers(_TARGET_A, raw_jwt="")
    h2 = await connector.auth_headers(_TARGET_A, raw_jwt="")

    assert h1 == h2
    assert call_count == 1
    await connector.aclose()


# ---------------------------------------------------------------------------
# Per-target isolation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_per_target_isolation_keeps_credentials_separate() -> None:
    """Two targets get two distinct credential cache entries; no cross-target leakage."""
    call_log: list[str] = []

    async def _tracking_loader(target: VcfFleetTargetLike) -> dict[str, str]:
        call_log.append(target.name)
        return {"username": f"svc-{target.name}", "password": "pass"}

    connector = VcfFleetConnector(credentials_loader=_tracking_loader)
    h_a = await connector.auth_headers(_TARGET_A, raw_jwt="")
    h_b = await connector.auth_headers(_TARGET_B, raw_jwt="")

    username_a, _ = _decode_basic_auth(h_a["Authorization"])
    username_b, _ = _decode_basic_auth(h_b["Authorization"])
    assert username_a == "svc-vcf-fleet-a"
    assert username_b == "svc-vcf-fleet-b"
    assert call_log == ["vcf-fleet-a", "vcf-fleet-b"]
    await connector.aclose()


# ---------------------------------------------------------------------------
# Credential loading failure modes — missing-key contract from CredentialsCache
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_loader_missing_password_key_raises_runtime_error_naming_target() -> None:
    """Loader returning a dict missing 'password' raises RuntimeError naming the target."""

    async def _bad_loader(_target: VcfFleetTargetLike) -> dict[str, str]:
        return {"username": "admin@local"}  # type: ignore[return-value]

    connector = VcfFleetConnector(credentials_loader=_bad_loader)
    with pytest.raises(RuntimeError, match=r"password") as exc_info:
        await connector.auth_headers(_TARGET_A, raw_jwt="")

    assert "vcf-fleet-a" in str(exc_info.value)
    await connector.aclose()


@pytest.mark.asyncio
async def test_loader_missing_username_key_raises_runtime_error_naming_target() -> None:
    """Loader returning a dict missing 'username' raises RuntimeError naming the target."""

    async def _bad_loader(_target: VcfFleetTargetLike) -> dict[str, str]:
        return {"password": "stub-password"}  # type: ignore[return-value]

    connector = VcfFleetConnector(credentials_loader=_bad_loader)
    with pytest.raises(RuntimeError, match=r"username") as exc_info:
        await connector.auth_headers(_TARGET_A, raw_jwt="")

    assert "vcf-fleet-a" in str(exc_info.value)
    await connector.aclose()


# ---------------------------------------------------------------------------
# Auth model gating
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "auth_model",
    [AuthModel.PER_USER.value, AuthModel.IMPERSONATION.value, "unknown-mode"],
)
async def test_auth_headers_rejects_non_shared_service_account_modes(auth_model: str) -> None:
    """Per-user / impersonation modes raise NotImplementedError naming the target + mode."""
    target = _StubTarget(
        name="vcf-fleet-per-user",
        host="vcf-fleet.test.invalid",
        port=443,
        secret_ref="kv/data/vcf-fleet/per-user",
        auth_model=auth_model,
    )
    connector = _make_connector()

    with pytest.raises(NotImplementedError) as exc_info:
        await connector.auth_headers(target, raw_jwt="")

    assert "vcf-fleet-per-user" in str(exc_info.value)
    assert auth_model in str(exc_info.value)
    await connector.aclose()


@pytest.mark.asyncio
async def test_auth_headers_accepts_none_auth_model_for_pre_g03_targets() -> None:
    """auth_model=None (pre-G0.3 column-not-yet-populated) is accepted."""
    target = _StubTarget(
        name="vcf-fleet-pre-g03",
        host="vcf-fleet.test.invalid",
        port=443,
        secret_ref="kv/data/vcf-fleet/pre-g03",
        auth_model=None,
    )
    connector = _make_connector()
    headers = await connector.auth_headers(target, raw_jwt="")
    assert headers["Authorization"].startswith("Basic ")
    await connector.aclose()


@pytest.mark.asyncio
async def test_auth_headers_accepts_enum_member_for_auth_model() -> None:
    """An AuthModel enum member (not just its string value) is accepted."""
    target = _StubTarget(
        name="vcf-fleet-enum",
        host="vcf-fleet.test.invalid",
        port=443,
        secret_ref="kv/data/vcf-fleet/enum",
    )
    target.auth_model = AuthModel.SHARED_SERVICE_ACCOUNT  # type: ignore[assignment]
    connector = _make_connector()
    headers = await connector.auth_headers(target, raw_jwt="")
    assert headers["Authorization"].startswith("Basic ")
    await connector.aclose()


# ---------------------------------------------------------------------------
# fingerprint() — wrapper-verified probe call against the datacenters surface
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fingerprint_canonical_shape_on_reachable_target() -> None:
    """fingerprint() against mocked datacenters returns the canonical shape with extras."""
    connector = _make_connector()

    async with respx.mock(base_url="https://vcf-fleet-a.test.invalid") as mock:
        mock.get("/lcm/lcops/api/v2/datacenters").respond(
            200,
            json=[
                {"datacenterName": "dc-01", "vmid": "abc"},
                {"datacenterName": "dc-02", "vmid": "def"},
            ],
            headers={"Lcm-API-Version": "8.0"},
        )
        fp = await connector.fingerprint(_TARGET_A)

    assert fp.vendor == "vmware"
    assert fp.product == "vcf-fleet"
    # Fleet exposes no product version via a working endpoint in 9.0;
    # the connector carries the LCM API version in `version` as the
    # only version string the wrapper-verified probe surfaces.
    assert fp.version == "8.0"
    assert fp.build is None
    assert fp.reachable is True
    assert "/lcm/lcops/api/v2/datacenters" in fp.probe_method
    assert "Lcm-API-Version" in fp.probe_method
    assert fp.extras["lcm_api_version"] == "8.0"
    assert fp.extras["datacenter_count"] == 2
    assert fp.extras["product_lineage"] == "vmware-vrealize-suite-lifecycle-manager"
    # The known-broken-diagnostic inventory ships with every fingerprint
    # so the next operator probing this product sees it inline.
    broken = fp.extras["diagnostic_endpoints_broken"]
    assert "/lcm/lcops/api/v2/about" in broken
    assert "/lcm/lcops/api/v2/health" in broken
    await connector.aclose()


@pytest.mark.asyncio
async def test_fingerprint_without_lcm_api_version_header_leaves_version_none() -> None:
    """A response without the Lcm-API-Version header leaves version=None."""
    connector = _make_connector()

    async with respx.mock(base_url="https://vcf-fleet-a.test.invalid") as mock:
        mock.get("/lcm/lcops/api/v2/datacenters").respond(200, json=[])
        fp = await connector.fingerprint(_TARGET_A)

    assert fp.version is None
    assert fp.reachable is True
    assert fp.extras["lcm_api_version"] is None
    assert fp.extras["datacenter_count"] == 0
    await connector.aclose()


@pytest.mark.asyncio
async def test_fingerprint_sends_basic_auth_header_to_datacenters() -> None:
    """The fingerprint call carries the Authorization: Basic header (admin@local)."""
    connector = _make_connector()
    captured: list[httpx.Request] = []

    async with respx.mock(base_url="https://vcf-fleet-a.test.invalid") as mock:
        route = mock.get("/lcm/lcops/api/v2/datacenters").respond(
            200,
            json=[],
            headers={"Lcm-API-Version": "8.0"},
        )
        await connector.fingerprint(_TARGET_A)
        captured.extend(call.request for call in route.calls)

    assert len(captured) == 1
    auth_header = captured[0].headers.get("Authorization", "")
    username, password = _decode_basic_auth(auth_header)
    assert username == "admin@local"
    assert password == "stub-password"
    await connector.aclose()


@pytest.mark.asyncio
async def test_fingerprint_unreachable_on_500_returns_reachable_false() -> None:
    """A 500 from the datacenters call surfaces as reachable=False + structured error.

    This is the wrapper-documented Fleet failure mode (the appliance's
    bootstrap not finished); the connector treats it the same way it
    treats any other transport/status failure.
    """
    connector = _make_connector()

    async with respx.mock(base_url="https://vcf-fleet-a.test.invalid") as mock:
        mock.get("/lcm/lcops/api/v2/datacenters").respond(500, json={"error": "bootstrap"})
        fp = await connector.fingerprint(_TARGET_A)

    assert fp.vendor == "vmware"
    assert fp.product == "vcf-fleet"
    assert fp.reachable is False
    error = fp.extras["error"]
    assert "HTTPStatusError" in error or "500" in error
    await connector.aclose()


@pytest.mark.asyncio
async def test_fingerprint_unreachable_on_401_returns_reachable_false() -> None:
    """A 401 from the datacenters call (wrong creds) surfaces as reachable=False."""
    connector = _make_connector()

    async with respx.mock(base_url="https://vcf-fleet-a.test.invalid") as mock:
        mock.get("/lcm/lcops/api/v2/datacenters").respond(
            401, json={"errors": [{"code": "UNAUTHORIZED"}]}
        )
        fp = await connector.fingerprint(_TARGET_A)

    assert fp.reachable is False
    error = fp.extras["error"]
    assert "HTTPStatusError" in error or "401" in error
    await connector.aclose()


@pytest.mark.asyncio
async def test_fingerprint_unreachable_on_connection_error_returns_reachable_false() -> None:
    """A transport-level connection error surfaces as reachable=False + structured error."""
    connector = _make_connector()

    async with respx.mock(base_url="https://vcf-fleet-a.test.invalid") as mock:
        mock.get("/lcm/lcops/api/v2/datacenters").mock(
            side_effect=httpx.ConnectError("connection refused")
        )
        fp = await connector.fingerprint(_TARGET_A)

    assert fp.reachable is False
    assert "ConnectError" in fp.extras["error"]
    await connector.aclose()


# ---------------------------------------------------------------------------
# probe() — delegates to fingerprint
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_probe_ok_when_fingerprint_reachable() -> None:
    """probe() returns ok=True when fingerprint reports reachable."""
    connector = _make_connector()

    async with respx.mock(base_url="https://vcf-fleet-a.test.invalid") as mock:
        mock.get("/lcm/lcops/api/v2/datacenters").respond(
            200, json=[], headers={"Lcm-API-Version": "8.0"}
        )
        probe = await connector.probe(_TARGET_A)

    assert probe.ok is True
    assert probe.reason is None
    await connector.aclose()


@pytest.mark.asyncio
async def test_probe_not_ok_when_fingerprint_unreachable() -> None:
    """probe() returns ok=False + reason from fingerprint's error extras."""
    connector = _make_connector()

    async with respx.mock(base_url="https://vcf-fleet-a.test.invalid") as mock:
        mock.get("/lcm/lcops/api/v2/datacenters").respond(500, json={"error": "bootstrap"})
        probe = await connector.probe(_TARGET_A)

    assert probe.ok is False
    assert probe.reason is not None
    assert "HTTPStatusError" in probe.reason or "500" in probe.reason
    await connector.aclose()


# ---------------------------------------------------------------------------
# aclose — credential cache is cleared
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_aclose_clears_credential_cache() -> None:
    """aclose() empties the shared credential cache so a reuse re-fetches."""
    call_count = 0

    async def _counting_loader(_target: VcfFleetTargetLike) -> dict[str, str]:
        nonlocal call_count
        call_count += 1
        return {"username": "admin@local", "password": "stub-password"}

    connector = VcfFleetConnector(credentials_loader=_counting_loader)
    await connector.auth_headers(_TARGET_A, raw_jwt="")
    assert call_count == 1
    await connector.aclose()
    await connector.auth_headers(_TARGET_A, raw_jwt="")
    assert call_count == 2
    await connector.aclose()
