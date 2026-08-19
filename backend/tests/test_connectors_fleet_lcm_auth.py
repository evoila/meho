# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Unit tests for :class:`FleetLcmConnector` auth + fingerprint/probe (#3036).

Exercises the modern Fleet LCM Service connector's auth seam — Bearer as the
primary scheme (``securitySchemes.bearerToken``) with Basic fallback
(``securitySchemes.basicAuth``) — per-target credential isolation, the
auth_model boundary gate, and the fingerprint/probe shapes against the
``GET /v1/health`` reachability probe.

No live appliance is reachable (#1002 / #995), so the Bearer *header shape* is
proven via an injected loader returning a ``"token"`` and the transport is
mocked with ``respx``; live Bearer verification is the documented follow-up.

Test layout mirrors :mod:`tests.test_connectors_vcf_fleet_auth` (the legacy
impl's auth test) — the shared ``CredentialsCache`` / ``basic_auth_header`` /
``is_acceptable_auth_model`` scaffolding and the fixture-clean registry
pattern.
"""

from __future__ import annotations

import base64
from collections.abc import Iterator
from dataclasses import dataclass, field
from uuid import UUID, uuid4

import httpx
import pytest
import respx

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors.adapters.http import HttpConnector
from meho_backplane.connectors.fleet_lcm import (
    FleetLcmConnector,
    FleetLcmTargetLike,
)
from meho_backplane.connectors.registry import (
    clear_registry,
    register_connector_v2,
)
from meho_backplane.connectors.schemas import AuthModel


def _make_operator(raw_jwt: str = "op.test.jwt") -> Operator:
    """Return a minimal :class:`Operator` for threading through the auth surface."""
    return Operator(
        sub="test-operator",
        name=None,
        email=None,
        raw_jwt=raw_jwt,
        tenant_id=UUID(int=0),
        tenant_role=TenantRole.OPERATOR,
    )


@pytest.fixture(autouse=True)
def _clean_fleet_lcm_registry() -> Iterator[None]:
    """Re-register FleetLcmConnector after sibling tests clear the registry.

    Same pattern :mod:`tests.test_connectors_vcf_fleet_auth` established: a
    sibling autouse fixture calls :func:`clear_registry` between tests, so
    re-register before every test in this module and clear after.
    """
    clear_registry()
    register_connector_v2(
        product=FleetLcmConnector.product,
        version=FleetLcmConnector.version,
        impl_id=FleetLcmConnector.impl_id,
        cls=FleetLcmConnector,
    )
    yield
    clear_registry()


# ---------------------------------------------------------------------------
# Target stub — satisfies FleetLcmTargetLike Protocol structurally.
# ---------------------------------------------------------------------------


@dataclass
class _StubTarget:
    name: str
    host: str
    port: int | None
    secret_ref: str
    auth_model: str | None = AuthModel.SHARED_SERVICE_ACCOUNT.value
    id: UUID = field(default_factory=uuid4)
    tenant_id: UUID = field(default_factory=lambda: UUID(int=0))


_TARGET_A = _StubTarget(
    name="fleet-lcm-a",
    host="fleet-lcm-a.test.invalid",
    port=443,
    secret_ref="fleet-lcm/fleet-lcm-a",
)
_TARGET_B = _StubTarget(
    name="fleet-lcm-b",
    host="fleet-lcm-b.test.invalid",
    port=443,
    secret_ref="fleet-lcm/fleet-lcm-b",
)


async def _basic_loader(_target: FleetLcmTargetLike, _operator: Operator) -> dict[str, str]:
    """Return canned username/password (no token) → Basic fallback path."""
    return {"username": "admin@local", "password": "stub-password"}


async def _bearer_loader(_target: FleetLcmTargetLike, _operator: Operator) -> dict[str, str]:
    """Return canned credentials carrying a ``token`` → Bearer primary path.

    Carries username/password too (the ``CredentialsCache`` contract requires
    them), plus the ``token`` that activates the Bearer header.
    """
    return {"username": "admin@local", "password": "stub-password", "token": "tok-abc123"}


def _make_connector() -> FleetLcmConnector:
    """Build a connector wired with the Basic (no-token) stub loader."""
    return FleetLcmConnector(credentials_loader=_basic_loader)


def _decode_basic_auth(authorization_header: str) -> tuple[str, str]:
    """Decode an ``Authorization: Basic <b64>`` header into (username, password)."""
    assert authorization_header.startswith("Basic ")
    decoded = base64.b64decode(authorization_header[6:]).decode()
    username, _, password = decoded.partition(":")
    return username, password


# ---------------------------------------------------------------------------
# ABC + registration plumbing
# ---------------------------------------------------------------------------


def test_fleet_lcm_connector_subclasses_http_connector() -> None:
    """Sanity check: the connector inherits from HttpConnector with the right metadata."""
    assert issubclass(FleetLcmConnector, HttpConnector)
    assert FleetLcmConnector.product == "fleet"
    assert FleetLcmConnector.version == "9.0"
    assert FleetLcmConnector.impl_id == "fleet-lcm"
    assert FleetLcmConnector.supported_version_range == ">=9.0,<10.0"
    # Equal to the legacy fleet-rest impl by design — the split is by
    # version-specificity, not priority.
    assert FleetLcmConnector.priority == 1


def test_importing_package_registers_versioned_triple_only() -> None:
    """The package registers the versioned triple and NOT a wildcard sibling.

    The legacy vcf_fleet package owns the ``("fleet","","")`` wildcard; a
    second class on that key would raise at import. This connector registers
    only ``("fleet","9.0","fleet-lcm")``.
    """
    from meho_backplane.connectors.fleet_lcm import (
        FLEET_LCM_CONNECTOR_ID,
        FLEET_LCM_IMPL_ID,
        FLEET_LCM_PRODUCT,
        FLEET_LCM_VERSION,
    )
    from meho_backplane.connectors.registry import all_connectors_v2

    registry = all_connectors_v2()
    versioned = ("fleet", "9.0", "fleet-lcm")
    assert registry[versioned] is FleetLcmConnector
    # No wildcard was registered by *this* module's fixture (the fixture only
    # re-registers the versioned triple), and the constants round-trip.
    assert versioned == (FLEET_LCM_PRODUCT, FLEET_LCM_VERSION, FLEET_LCM_IMPL_ID)
    assert FLEET_LCM_CONNECTOR_ID == "fleet-lcm-9.0"


# ---------------------------------------------------------------------------
# Auth — Bearer primary + Basic fallback
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_auth_headers_emits_bearer_when_token_present() -> None:
    """A loader returning a ``token`` yields ``Authorization: Bearer <token>``."""
    connector = FleetLcmConnector(credentials_loader=_bearer_loader)
    headers = await connector.auth_headers(_TARGET_A, operator=_make_operator())
    assert headers == {"Authorization": "Bearer tok-abc123"}
    await connector.aclose()


@pytest.mark.asyncio
async def test_auth_headers_falls_back_to_basic_without_token() -> None:
    """A loader returning only username/password yields the Basic header (spec's basicAuth)."""
    connector = _make_connector()
    headers = await connector.auth_headers(_TARGET_A, operator=_make_operator())

    assert "Authorization" in headers
    assert headers["Authorization"].startswith("Basic ")
    username, password = _decode_basic_auth(headers["Authorization"])
    assert username == "admin@local"
    assert password == "stub-password"
    await connector.aclose()


@pytest.mark.asyncio
async def test_auth_headers_reuses_cached_credentials_across_calls() -> None:
    """Second auth_headers call against the same target does NOT re-invoke the loader."""
    call_count = 0

    async def _counting_loader(_target: FleetLcmTargetLike, _operator: Operator) -> dict[str, str]:
        nonlocal call_count
        call_count += 1
        return {"username": "admin@local", "password": "stub-password"}

    connector = FleetLcmConnector(credentials_loader=_counting_loader)
    h1 = await connector.auth_headers(_TARGET_A, operator=_make_operator())
    h2 = await connector.auth_headers(_TARGET_A, operator=_make_operator())

    assert h1 == h2
    assert call_count == 1
    await connector.aclose()


@pytest.mark.asyncio
async def test_per_target_isolation_keeps_credentials_separate() -> None:
    """Two targets get two distinct credential cache entries; no cross-target leakage."""
    call_log: list[str] = []

    async def _tracking_loader(target: FleetLcmTargetLike, _operator: Operator) -> dict[str, str]:
        call_log.append(target.name)
        return {"username": f"svc-{target.name}", "password": "pass"}

    connector = FleetLcmConnector(credentials_loader=_tracking_loader)
    h_a = await connector.auth_headers(_TARGET_A, operator=_make_operator())
    h_b = await connector.auth_headers(_TARGET_B, operator=_make_operator())

    username_a, _ = _decode_basic_auth(h_a["Authorization"])
    username_b, _ = _decode_basic_auth(h_b["Authorization"])
    assert username_a == "svc-fleet-lcm-a"
    assert username_b == "svc-fleet-lcm-b"
    assert call_log == ["fleet-lcm-a", "fleet-lcm-b"]
    await connector.aclose()


@pytest.mark.asyncio
async def test_loader_missing_password_key_raises_runtime_error_naming_target() -> None:
    """Loader returning a dict missing 'password' raises RuntimeError naming the target.

    The shared ``CredentialsCache`` contract holds even on the Bearer path —
    username/password are always required (the appliance accepts basicAuth),
    so a token-only secret is a misconfiguration surfaced here.
    """

    async def _bad_loader(_target: FleetLcmTargetLike, _operator: Operator) -> dict[str, str]:
        return {"token": "tok-only"}

    connector = FleetLcmConnector(credentials_loader=_bad_loader)
    with pytest.raises(RuntimeError, match=r"password") as exc_info:
        await connector.auth_headers(_TARGET_A, operator=_make_operator())

    assert "fleet-lcm-a" in str(exc_info.value)
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
        name="fleet-lcm-per-user",
        host="fleet-lcm.test.invalid",
        port=443,
        secret_ref="fleet-lcm/per-user",
        auth_model=auth_model,
    )
    connector = _make_connector()

    with pytest.raises(NotImplementedError) as exc_info:
        await connector.auth_headers(target, operator=_make_operator())

    assert "fleet-lcm-per-user" in str(exc_info.value)
    assert auth_model in str(exc_info.value)
    await connector.aclose()


@pytest.mark.asyncio
async def test_auth_headers_accepts_none_auth_model_for_pre_g03_targets() -> None:
    """auth_model=None (pre-G0.3 column-not-yet-populated) is accepted."""
    target = _StubTarget(
        name="fleet-lcm-pre-g03",
        host="fleet-lcm.test.invalid",
        port=443,
        secret_ref="fleet-lcm/pre-g03",
        auth_model=None,
    )
    connector = _make_connector()
    headers = await connector.auth_headers(target, operator=_make_operator())
    assert headers["Authorization"].startswith("Basic ")
    await connector.aclose()


# ---------------------------------------------------------------------------
# fingerprint() — GET /v1/health probe
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fingerprint_canonical_shape_on_reachable_target() -> None:
    """fingerprint() against a mocked /v1/health returns the canonical reachable shape."""
    connector = _make_connector()

    async with respx.mock(base_url="https://fleet-lcm-a.test.invalid") as mock:
        mock.get("/v1/health").respond(200, json={"status": "HEALTHY", "version": "9.0.0.0"})
        fp = await connector.fingerprint(_TARGET_A)

    assert fp.vendor == "vmware"
    assert fp.product == "fleet"
    assert fp.version == "9.0.0.0"
    assert fp.build is None
    assert fp.reachable is True
    assert "/v1/health" in fp.probe_method
    assert fp.extras["health_status"] == "HEALTHY"
    assert fp.extras["api_surface"] == "fleet-lcm-v1"
    await connector.aclose()


@pytest.mark.asyncio
async def test_fingerprint_without_version_field_leaves_version_none() -> None:
    """A health payload without a ``version`` field leaves version=None but reachable."""
    connector = _make_connector()

    async with respx.mock(base_url="https://fleet-lcm-a.test.invalid") as mock:
        mock.get("/v1/health").respond(200, json={"status": "HEALTHY"})
        fp = await connector.fingerprint(_TARGET_A)

    assert fp.version is None
    assert fp.reachable is True
    assert fp.extras["health_status"] == "HEALTHY"
    await connector.aclose()


@pytest.mark.asyncio
async def test_fingerprint_sends_bearer_header_to_health_when_token_present() -> None:
    """The fingerprint call carries the Bearer header when the loader yields a token."""
    connector = FleetLcmConnector(credentials_loader=_bearer_loader)
    captured: list[httpx.Request] = []

    async with respx.mock(base_url="https://fleet-lcm-a.test.invalid") as mock:
        route = mock.get("/v1/health").respond(200, json={"status": "HEALTHY"})
        await connector.fingerprint(_TARGET_A)
        captured.extend(call.request for call in route.calls)

    assert len(captured) == 1
    assert captured[0].headers.get("Authorization") == "Bearer tok-abc123"
    await connector.aclose()


@pytest.mark.asyncio
async def test_fingerprint_unreachable_on_500_returns_reachable_false() -> None:
    """A 500 from /v1/health surfaces as reachable=False + structured error."""
    connector = _make_connector()

    async with respx.mock(base_url="https://fleet-lcm-a.test.invalid") as mock:
        mock.get("/v1/health").respond(500, json={"error": "bootstrap"})
        fp = await connector.fingerprint(_TARGET_A)

    assert fp.vendor == "vmware"
    assert fp.product == "fleet"
    assert fp.reachable is False
    error = fp.extras["error"]
    assert "HTTPStatusError" in error or "500" in error
    await connector.aclose()


@pytest.mark.asyncio
async def test_fingerprint_unreachable_on_connection_error_returns_reachable_false() -> None:
    """A transport-level connection error surfaces as reachable=False + structured error."""
    connector = _make_connector()

    async with respx.mock(base_url="https://fleet-lcm-a.test.invalid") as mock:
        mock.get("/v1/health").mock(side_effect=httpx.ConnectError("connection refused"))
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

    async with respx.mock(base_url="https://fleet-lcm-a.test.invalid") as mock:
        mock.get("/v1/health").respond(200, json={"status": "HEALTHY"})
        probe = await connector.probe(_TARGET_A)

    assert probe.ok is True
    assert probe.reason is None
    await connector.aclose()


@pytest.mark.asyncio
async def test_probe_not_ok_when_fingerprint_unreachable() -> None:
    """probe() returns ok=False + reason from fingerprint's error extras."""
    connector = _make_connector()

    async with respx.mock(base_url="https://fleet-lcm-a.test.invalid") as mock:
        mock.get("/v1/health").respond(500, json={"error": "bootstrap"})
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

    async def _counting_loader(_target: FleetLcmTargetLike, _operator: Operator) -> dict[str, str]:
        nonlocal call_count
        call_count += 1
        return {"username": "admin@local", "password": "stub-password"}

    connector = FleetLcmConnector(credentials_loader=_counting_loader)
    await connector.auth_headers(_TARGET_A, operator=_make_operator())
    assert call_count == 1
    await connector.aclose()
    await connector.auth_headers(_TARGET_A, operator=_make_operator())
    assert call_count == 2
    await connector.aclose()
