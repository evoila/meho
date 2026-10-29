# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Unit tests for :class:`FleetLcmConnector` auth + fingerprint/probe (#3036, #4476).

Covers the login seam — the two-step VCF Operations login (``token/acquire``,
then ``token/exchange`` with the service key ``fleet-lcm``) that yields the
Bearer token, the stored-token fallback, the optional ``vcf_ops_url`` login
address, session renewal through :meth:`FleetLcmConnector.invalidate_session`,
per-target isolation, the auth_model gate, and the fingerprint/probe shapes
against ``GET /v1/health``. The transport is mocked with ``respx``; the
acquire and exchange answers are hand-made fixtures shaped like the vendor's
VCF Operations 9.1 API file.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import dataclass, field
from uuid import UUID, uuid4

import httpx
import pytest
import respx
from structlog.testing import capture_logs

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors._shared.vault_creds import VaultCredentialsReadError
from meho_backplane.connectors._shared.vcf_auth import ConnectorAuthError, SessionLoginError
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
    fqdn: str | None = None
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


_PASSWORD = "stub-password-must-not-leak"
_OPS_TOKEN = "ops-token-must-not-leak"
_JWT = "fleet-jwt-must-not-leak"
_ACQUIRE_PATH = "/suite-api/api/auth/token/acquire"
_EXCHANGE_PATH = "/suite-api/api/auth/token/exchange"


async def _login_loader(_target: FleetLcmTargetLike, _operator: Operator) -> dict[str, str]:
    """VCF Operations username/password only → the connector logs in."""
    return {"username": "admin", "password": _PASSWORD}


async def _bearer_loader(_target: FleetLcmTargetLike, _operator: Operator) -> dict[str, str]:
    """Credentials carrying a stored ``token`` → sent as is, no login."""
    return {"username": "admin", "password": _PASSWORD, "token": "tok-abc123"}


def _make_connector() -> FleetLcmConnector:
    """Build a connector with a stored token, so no login runs (fingerprint tests)."""
    return FleetLcmConnector(credentials_loader=_bearer_loader)


def _mock_login(
    mock: respx.MockRouter, *, jwt: str = _JWT, ops_token: str = _OPS_TOKEN
) -> tuple[respx.Route, respx.Route]:
    """Mock the acquire and exchange answers (vendor 9.1 shapes) on *mock*."""
    acquire = mock.post(_ACQUIRE_PATH).respond(
        200,
        json={"token": ops_token, "validity": 1767225600000, "expiresAt": "x", "roles": []},
    )
    exchange = mock.post(_EXCHANGE_PATH).respond(200, json={"jwtToken": jwt, "services": []})
    return acquire, exchange


def _paths_in_order(mock: respx.MockRouter) -> list[str]:
    """The request paths *mock* saw, in order."""
    return [call.request.url.path for call in mock.calls]


# ---------------------------------------------------------------------------
# ABC + registration plumbing
# ---------------------------------------------------------------------------


def test_fleet_lcm_connector_subclasses_http_connector() -> None:
    """Sanity check: the connector inherits from HttpConnector with the right metadata."""
    assert issubclass(FleetLcmConnector, HttpConnector)
    assert FleetLcmConnector.product == "fleet"
    assert FleetLcmConnector.version == "9.0"
    assert FleetLcmConnector.impl_id == "fleet-lcm"
    assert FleetLcmConnector.supported_version_range == ">=9.1,<10.0"
    # Equal to the legacy fleet-rest impl by design — the split is by
    # version range, not priority.
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
# Auth — VCF Operations login (acquire, then exchange) + stored-token fallback
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_login_acquires_then_exchanges_and_sends_bearer() -> None:
    """No stored token: acquire, then exchange with ``fleet-lcm``, then Bearer <jwtToken>."""
    connector = FleetLcmConnector(credentials_loader=_login_loader)

    async with respx.mock(base_url="https://fleet-lcm-a.test.invalid") as mock:
        acquire, exchange = _mock_login(mock)
        headers = await connector.auth_headers(_TARGET_A, operator=_make_operator())
        # Acquire runs before exchange.
        assert _paths_in_order(mock) == [_ACQUIRE_PATH, _EXCHANGE_PATH]

    assert headers == {"Authorization": f"Bearer {_JWT}"}
    assert acquire.call_count == 1 and exchange.call_count == 1
    acquire_request = acquire.calls[0].request
    assert json.loads(acquire_request.content) == {"username": "admin", "password": _PASSWORD}
    exchange_request = exchange.calls[0].request
    assert exchange_request.headers["Authorization"] == f"OpsToken {_OPS_TOKEN}"
    assert json.loads(exchange_request.content) == {"serviceKeys": ["fleet-lcm"]}
    await connector.aclose()


@pytest.mark.asyncio
async def test_stored_token_is_sent_without_login() -> None:
    """A stored ``token`` is sent as is; no login request is made (the fallback)."""
    connector = FleetLcmConnector(credentials_loader=_bearer_loader)
    async with respx.mock(base_url="https://fleet-lcm-a.test.invalid") as mock:
        headers = await connector.auth_headers(_TARGET_A, operator=_make_operator())
    assert headers == {"Authorization": "Bearer tok-abc123"}
    assert mock.calls.call_count == 0
    await connector.aclose()


@pytest.mark.asyncio
async def test_token_is_reused_until_invalidated() -> None:
    """The login runs once per target; invalidate_session makes the next call log in again."""
    call_count = 0

    async def _counting_loader(_target: FleetLcmTargetLike, _operator: Operator) -> dict[str, str]:
        nonlocal call_count
        call_count += 1
        return {"username": "admin", "password": _PASSWORD}

    connector = FleetLcmConnector(credentials_loader=_counting_loader)
    async with respx.mock(base_url="https://fleet-lcm-a.test.invalid") as mock:
        acquire, exchange = _mock_login(mock)
        exchange.side_effect = [
            httpx.Response(200, json={"jwtToken": "jwt-1"}),
            httpx.Response(200, json={"jwtToken": "jwt-2"}),
        ]
        h1 = await connector.auth_headers(_TARGET_A, operator=_make_operator())
        h2 = await connector.auth_headers(_TARGET_A, operator=_make_operator())
        await connector.invalidate_session(_TARGET_A)
        h3 = await connector.auth_headers(_TARGET_A, operator=_make_operator())

    assert h1 == h2 == {"Authorization": "Bearer jwt-1"}
    assert h3 == {"Authorization": "Bearer jwt-2"}
    assert acquire.call_count == 2 and exchange.call_count == 2
    # invalidate_session also re-reads Vault, so a replaced secret is picked up.
    assert call_count == 2
    await connector.aclose()


@pytest.mark.asyncio
async def test_per_target_isolation_keeps_tokens_separate() -> None:
    """Two targets log in at their own address and get their own token."""
    connector = FleetLcmConnector(credentials_loader=_login_loader)
    async with (
        respx.mock(base_url="https://fleet-lcm-a.test.invalid") as mock_a,
        respx.mock(base_url="https://fleet-lcm-b.test.invalid") as mock_b,
    ):
        _mock_login(mock_a, jwt="jwt-a")
        _mock_login(mock_b, jwt="jwt-b")
        h_a = await connector.auth_headers(_TARGET_A, operator=_make_operator())
        h_b = await connector.auth_headers(_TARGET_B, operator=_make_operator())

    assert h_a == {"Authorization": "Bearer jwt-a"}
    assert h_b == {"Authorization": "Bearer jwt-b"}
    await connector.aclose()


@pytest.mark.asyncio
async def test_login_goes_to_vcf_ops_url_when_set() -> None:
    """With ``vcf_ops_url`` the login runs on that address, not the target's."""

    async def _ops_url_loader(_target: FleetLcmTargetLike, _operator: Operator) -> dict[str, str]:
        return {
            "username": "admin",
            "password": _PASSWORD,
            "vcf_ops_url": "https://vcf-ops.test.invalid:8443",
        }

    connector = FleetLcmConnector(credentials_loader=_ops_url_loader)
    async with (
        respx.mock(base_url="https://vcf-ops.test.invalid:8443") as ops_mock,
        respx.mock(base_url="https://fleet-lcm-a.test.invalid", assert_all_called=False) as fleet,
    ):
        _mock_login(ops_mock)
        headers = await connector.auth_headers(_TARGET_A, operator=_make_operator())

    assert headers == {"Authorization": f"Bearer {_JWT}"}
    assert fleet.calls.call_count == 0
    await connector.aclose()


@pytest.mark.asyncio
async def test_targets_with_one_secret_and_vcf_ops_url_share_one_login_and_renewal() -> None:
    """Two targets on different hosts log in once at the shared ``vcf_ops_url``.

    The token is keyed by the login address from the secret, not by the
    target's own name, host or ``fqdn`` (the login does not send the
    ``fqdn``). A renewal on one target gives both the new token, and a
    ``401`` on the other target with the old token keeps the new one.
    """
    shared_secret = "fleet-lcm/shared"
    first = _StubTarget(
        name="fleet-lcm-a",
        host="fleet-lcm-a.test.invalid",
        port=443,
        secret_ref=shared_secret,
        fqdn="fleet-a.example.test",
    )
    second = _StubTarget(
        name="fleet-lcm-b",
        host="fleet-lcm-b.test.invalid/fleet-lcm",
        port=None,
        secret_ref=shared_secret,
        fqdn="fleet-b.example.test",
    )

    async def _ops_url_loader(_target: FleetLcmTargetLike, _operator: Operator) -> dict[str, str]:
        return {
            "username": "admin",
            "password": _PASSWORD,
            "vcf_ops_url": "https://vcf-ops.test.invalid",
        }

    connector = FleetLcmConnector(credentials_loader=_ops_url_loader)
    async with respx.mock(base_url="https://vcf-ops.test.invalid") as ops_mock:
        acquire, exchange = _mock_login(ops_mock)
        exchange.side_effect = [
            httpx.Response(200, json={"jwtToken": "jwt-1"}),
            httpx.Response(200, json={"jwtToken": "jwt-2"}),
        ]
        operator = _make_operator()
        h_a = await connector.auth_headers(first, operator=operator)
        h_b = await connector.auth_headers(second, operator=operator)
        assert h_a == h_b == {"Authorization": "Bearer jwt-1"}

        rejected_a = httpx.Request("GET", "https://fleet-lcm-a.test.invalid/v1/health", headers=h_a)
        await connector.invalidate_session(first, rejected=rejected_a)
        h_a2 = await connector.auth_headers(first, operator=operator)
        # A late 401 on the other target still carries the old token: the new one stays.
        rejected_b = httpx.Request("GET", "https://fleet-lcm-b.test.invalid/v1/health", headers=h_b)
        await connector.invalidate_session(second, rejected=rejected_b)
        h_b2 = await connector.auth_headers(second, operator=operator)

    assert h_a2 == h_b2 == {"Authorization": "Bearer jwt-2"}
    assert acquire.call_count == 2 and exchange.call_count == 2
    await connector.aclose()


@pytest.mark.asyncio
async def test_login_goes_to_ipv6_vcf_ops_url_with_explicit_port() -> None:
    """An IPv6 ``vcf_ops_url`` keeps its brackets, so the login client address is valid."""

    async def _ops_url_loader(_target: FleetLcmTargetLike, _operator: Operator) -> dict[str, str]:
        return {
            "username": "admin",
            "password": _PASSWORD,
            "vcf_ops_url": "https://[fd00::10]:8443",
        }

    connector = FleetLcmConnector(credentials_loader=_ops_url_loader)
    async with (
        respx.mock(base_url="https://[fd00::10]:8443") as ops_mock,
        respx.mock(base_url="https://fleet-lcm-a.test.invalid", assert_all_called=False) as fleet,
    ):
        _mock_login(ops_mock)
        headers = await connector.auth_headers(_TARGET_A, operator=_make_operator())
        assert _paths_in_order(ops_mock) == [_ACQUIRE_PATH, _EXCHANGE_PATH]
        acquire_url = ops_mock.calls[0].request.url
        assert (acquire_url.host, acquire_url.port) == ("fd00::10", 8443)
        assert fleet.calls.call_count == 0

    assert headers == {"Authorization": f"Bearer {_JWT}"}
    await connector.aclose()


@pytest.mark.asyncio
async def test_login_skips_the_service_path_on_the_target_host() -> None:
    """A ``/fleet-lcm`` service path on ``host`` is used for fleet calls, not for the login."""
    target = _StubTarget(
        name="fleet-lcm-prefixed",
        host="fleet-lcm-a.test.invalid/fleet-lcm",
        port=443,
        secret_ref="fleet-lcm/fleet-lcm-prefixed",
    )
    connector = FleetLcmConnector(credentials_loader=_login_loader)
    async with respx.mock(base_url="https://fleet-lcm-a.test.invalid") as mock:
        _mock_login(mock)
        mock.get("/fleet-lcm/v1/health").respond(200, json={"status": "HEALTHY"})
        fp = await connector.fingerprint(target, operator=_make_operator())
        assert _paths_in_order(mock) == [_ACQUIRE_PATH, _EXCHANGE_PATH, "/fleet-lcm/v1/health"]

    assert fp.reachable is True
    await connector.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("step", ["acquire", "exchange"])
@pytest.mark.parametrize("status", [401, 403])
async def test_refused_login_raises_connector_auth_error(step: str, status: int) -> None:
    """A 401/403 at acquire or exchange is an auth failure naming the target, no secret."""
    connector = FleetLcmConnector(credentials_loader=_login_loader)
    async with respx.mock(
        base_url="https://fleet-lcm-a.test.invalid", assert_all_called=False
    ) as mock:
        acquire, exchange = _mock_login(mock)
        (acquire if step == "acquire" else exchange).respond(status)
        with pytest.raises(ConnectorAuthError) as exc_info:
            await connector.auth_headers(_TARGET_A, operator=_make_operator())

    assert exc_info.value.status_code == status
    message = str(exc_info.value)
    assert "fleet-lcm-a" in message
    for secret in (_PASSWORD, _OPS_TOKEN, _JWT):
        assert secret not in message
    await connector.aclose()


@pytest.mark.asyncio
async def test_exchange_without_jwt_token_raises_session_login_error() -> None:
    """A 200 exchange answer with no ``jwtToken`` fails clearly, with no token in the text."""
    connector = FleetLcmConnector(credentials_loader=_login_loader)
    async with respx.mock(base_url="https://fleet-lcm-a.test.invalid") as mock:
        _acquire, exchange = _mock_login(mock)
        exchange.respond(200, json={"services": []})
        with pytest.raises(SessionLoginError, match="no jwtToken") as exc_info:
            await connector.auth_headers(_TARGET_A, operator=_make_operator())

    assert not isinstance(exc_info.value, ConnectorAuthError)
    assert _OPS_TOKEN not in str(exc_info.value)
    await connector.aclose()


@pytest.mark.asyncio
async def test_login_never_logs_a_token_or_password() -> None:
    """No password or token value appears in any log event of the login."""
    connector = FleetLcmConnector(credentials_loader=_login_loader)
    with capture_logs() as captured:
        async with respx.mock(base_url="https://fleet-lcm-a.test.invalid") as mock:
            _mock_login(mock)
            await connector.auth_headers(_TARGET_A, operator=_make_operator())

    assert any(event["event"] == "fleet_lcm_session_established" for event in captured)
    log_blob = repr(captured)
    for secret in (_PASSWORD, _OPS_TOKEN, _JWT):
        assert secret not in log_blob
    await connector.aclose()


@pytest.mark.asyncio
async def test_kept_token_is_not_served_to_a_caller_without_jwt() -> None:
    """A token kept for an operator is refused to a system caller with no JWT."""
    connector = FleetLcmConnector(credentials_loader=_login_loader)
    async with respx.mock(base_url="https://fleet-lcm-a.test.invalid") as mock:
        _mock_login(mock)
        await connector.auth_headers(_TARGET_A, operator=_make_operator())
        with pytest.raises(VaultCredentialsReadError):
            await connector.auth_headers(_TARGET_A, operator=_make_operator(raw_jwt=""))
    await connector.aclose()


@pytest.mark.asyncio
async def test_loader_missing_password_key_raises_runtime_error_naming_target() -> None:
    """Loader returning a dict missing 'password' raises RuntimeError naming the target.

    The shared ``CredentialsCache`` contract holds even with a stored token:
    the VCF Operations username/password are always required.
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
    assert headers == {"Authorization": "Bearer tok-abc123"}
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
        return {"username": "admin", "password": _PASSWORD, "token": "tok-abc123"}

    connector = FleetLcmConnector(credentials_loader=_counting_loader)
    await connector.auth_headers(_TARGET_A, operator=_make_operator())
    assert call_count == 1
    await connector.aclose()
    await connector.auth_headers(_TARGET_A, operator=_make_operator())
    assert call_count == 2
    await connector.aclose()
