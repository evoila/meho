# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Unit tests for :class:`SddcManagerConnector` auth + fingerprint/probe (G3.5-T4 #616).

Exercises HTTP Basic auth with sso_realm handling (default + override),
per-target credential isolation, the auth_model boundary gate, and the
fingerprint/probe shape against a mocked ``GET /v1/sddc-managers`` endpoint.

Auth divergence from the NSX/vSphere precedents: no session token is
established — HTTP Basic is sent on every request via
``Authorization: Basic <base64(username@realm:password)>``. Credentials are
cached per target so Vault is only queried once per target per connector
instance lifetime.
"""

from __future__ import annotations

import base64
from collections.abc import Iterator
from dataclasses import dataclass, field
from uuid import UUID

import pytest
import respx

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors._shared.system_operator import (
    SYSTEM_OPERATOR_SUB,
    synthesise_system_operator,
)
from meho_backplane.connectors._shared.vault_creds import VaultCredentialsReadError
from meho_backplane.connectors.adapters.http import HttpConnector
from meho_backplane.connectors.registry import (
    clear_registry,
    register_connector_v2,
)
from meho_backplane.connectors.schemas import AuthModel
from meho_backplane.connectors.sddc_manager import (
    SddcManagerConnector,
    SddcTargetLike,
)


def _make_operator(raw_jwt: str = "") -> Operator:
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
def _clean_sddc_registry() -> Iterator[None]:
    """Re-register SddcManagerConnector after sibling tests clear the registry.

    ``test_connectors_registry_v2.py`` installs an autouse fixture that
    calls :func:`clear_registry` between tests. Re-register before every
    test in this module and clear after — same pattern
    :mod:`tests.test_connectors_nsx_auth` established.
    """
    clear_registry()
    register_connector_v2(
        product=SddcManagerConnector.product,
        version=SddcManagerConnector.version,
        impl_id=SddcManagerConnector.impl_id,
        cls=SddcManagerConnector,
    )
    yield
    clear_registry()


# ---------------------------------------------------------------------------
# Target stub — satisfies SddcTargetLike Protocol structurally.
# Replaced by the real Target model when G0.3 (#224) lands.
# ---------------------------------------------------------------------------


@dataclass
class _StubTarget:
    name: str
    host: str
    port: int | None
    secret_ref: str
    auth_model: str | None = AuthModel.SHARED_SERVICE_ACCOUNT.value
    sso_realm: str = field(default="vsphere.local")


_TARGET_A = _StubTarget(
    name="sddc-a",
    host="sddc-a.test.invalid",
    port=443,
    secret_ref="sddc/sddc-a",
)
_TARGET_B = _StubTarget(
    name="sddc-b",
    host="sddc-b.test.invalid",
    port=443,
    secret_ref="sddc/sddc-b",
)


async def _stub_loader(_target: SddcTargetLike, _operator: Operator) -> dict[str, str]:
    """Return canned credentials regardless of the target or operator."""
    return {"username": "svc-meho", "password": "stub-password"}


def _make_connector() -> SddcManagerConnector:
    """Build a connector wired with the stub loader."""
    return SddcManagerConnector(credentials_loader=_stub_loader)


def _decode_basic_auth(authorization_header: str) -> tuple[str, str]:
    """Decode an ``Authorization: Basic <b64>`` header into (username, password)."""
    assert authorization_header.startswith("Basic ")
    decoded = base64.b64decode(authorization_header[6:]).decode()
    username, _, password = decoded.partition(":")
    return username, password


# ---------------------------------------------------------------------------
# ABC + registration plumbing
# ---------------------------------------------------------------------------


def test_sddc_connector_subclasses_http_connector() -> None:
    """Sanity check: the connector inherits from HttpConnector with the right metadata."""
    assert issubclass(SddcManagerConnector, HttpConnector)
    assert SddcManagerConnector.product == "sddc-manager"
    assert SddcManagerConnector.version == "9.0"
    assert SddcManagerConnector.impl_id == "sddc-rest"
    assert SddcManagerConnector.supported_version_range == ">=9.0,<10.0"
    assert SddcManagerConnector.priority == 1


def test_importing_package_registers_against_v2_registry() -> None:
    """The package's __init__ calls register_connector_v2 at import time."""
    from meho_backplane.connectors.registry import all_connectors_v2

    registry = all_connectors_v2()
    key = ("sddc-manager", "9.0", "sddc-rest")
    assert key in registry
    assert registry[key] is SddcManagerConnector


def test_default_credentials_loader_delegates_to_shared_basic_loader() -> None:
    """The default loader is the thin wrapper around ``load_basic_credentials``.

    G3.10-T1 (#945) wired the live read; the loader now delegates to
    :func:`load_basic_credentials` rather than raising
    :exc:`NotImplementedError`. The fail-closed precondition (empty
    ``operator.raw_jwt``) is asserted via a :class:`VaultCredentialsReadError`
    on a system-initiated synthetic operator (no Vault is touched).
    """
    import asyncio

    from meho_backplane.connectors._shared.vault_creds import VaultCredentialsReadError
    from meho_backplane.connectors.sddc_manager.session import load_credentials_from_vault

    async def _check() -> None:
        system_operator = _make_operator(raw_jwt="")
        with pytest.raises(VaultCredentialsReadError, match=r"system-initiated"):
            await load_credentials_from_vault(_TARGET_A, system_operator)

    asyncio.run(_check())


# ---------------------------------------------------------------------------
# HTTP Basic auth — happy paths
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_auth_headers_sends_basic_auth_with_default_sso_realm() -> None:
    """auth_headers() produces Authorization: Basic with sso_realm=vsphere.local default.

    The username in the Basic auth header must be ``svc-meho@vsphere.local``,
    not bare ``svc-meho``. This is the load-bearing sso_realm contract.

    auth_headers() does not make any HTTP calls — it computes the header from
    cached credentials — so no respx mock is needed here.
    """
    connector = _make_connector()
    headers = await connector.auth_headers(_TARGET_A, operator=_make_operator())

    assert "Authorization" in headers
    assert headers["Authorization"].startswith("Basic ")
    username, password = _decode_basic_auth(headers["Authorization"])
    assert username == "svc-meho@vsphere.local"
    assert password == "stub-password"
    await connector.aclose()


@pytest.mark.asyncio
async def test_auth_headers_honors_explicit_sso_realm_override() -> None:
    """A target with a custom sso_realm has that realm reflected in the Basic auth header."""
    target = _StubTarget(
        name="sddc-custom",
        host="sddc-custom.test.invalid",
        port=443,
        secret_ref="sddc/custom",
        sso_realm="corp.example.com",
    )
    connector = _make_connector()

    headers = await connector.auth_headers(target, operator=_make_operator())
    username, _ = _decode_basic_auth(headers["Authorization"])
    assert username == "svc-meho@corp.example.com"
    await connector.aclose()


@pytest.mark.asyncio
async def test_auth_headers_reuses_cached_credentials_across_calls() -> None:
    """Second auth_headers call against the same target does NOT re-invoke the loader."""
    call_count = 0

    async def _counting_loader(_target: SddcTargetLike, _operator: Operator) -> dict[str, str]:
        nonlocal call_count
        call_count += 1
        return {"username": "svc-meho", "password": "stub-password"}

    connector = SddcManagerConnector(credentials_loader=_counting_loader)
    h1 = await connector.auth_headers(_TARGET_A, operator=_make_operator())
    h2 = await connector.auth_headers(_TARGET_A, operator=_make_operator())

    assert h1 == h2
    assert call_count == 1
    await connector.aclose()


# ---------------------------------------------------------------------------
# System-operator cache-bypass (fail-closed; #1008)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_warm_cache_not_served_to_system_operator_runs_loader() -> None:
    """A warm cache primed by a real operator is NOT served to the system operator.

    The system/operator-less caller (``synthesise_system_operator``) must
    re-run the loader so its fail-closed behaviour applies — it can never
    borrow warm credentials a real operator resolved (#1008). Keyed off
    ``SYSTEM_OPERATOR_SUB``, not ``raw_jwt`` (the system operator carries a
    non-empty placeholder JWT since #980).
    """
    call_log: list[str] = []

    async def _counting_loader(_target: SddcTargetLike, operator: Operator) -> dict[str, str]:
        call_log.append(operator.sub)
        return {"username": "svc-meho", "password": "stub-password"}

    connector = SddcManagerConnector(credentials_loader=_counting_loader)
    # Real operator warms the cache (cold load).
    await connector.auth_headers(_TARGET_A, operator=_make_operator())
    # System operator must re-run the loader rather than reuse the warm cache.
    await connector.auth_headers(_TARGET_A, operator=synthesise_system_operator())

    assert call_log == ["test-operator", SYSTEM_OPERATOR_SUB]
    await connector.aclose()


@pytest.mark.asyncio
async def test_system_operator_fails_closed_against_warm_cache() -> None:
    """With a warm cache, a system-operator load runs the loader and fails closed.

    Proves the bypass is closed end to end: even though a real operator
    primed the cache, the system caller hits the loader, which fails closed
    per its contract (a system-initiated read cannot resolve per-target
    credentials).
    """

    async def _failing_for_system_loader(
        _target: SddcTargetLike, operator: Operator
    ) -> dict[str, str]:
        if operator.sub == SYSTEM_OPERATOR_SUB:
            raise VaultCredentialsReadError(
                "system-initiated calls cannot read per-target vendor credentials"
            )
        return {"username": "svc-meho", "password": "stub-password"}

    connector = SddcManagerConnector(credentials_loader=_failing_for_system_loader)
    await connector.auth_headers(_TARGET_A, operator=_make_operator())  # warm the cache
    with pytest.raises(VaultCredentialsReadError, match=r"system-initiated"):
        await connector.auth_headers(_TARGET_A, operator=synthesise_system_operator())
    await connector.aclose()


@pytest.mark.asyncio
async def test_real_operator_reuse_unchanged_after_system_operator_call() -> None:
    """Real-operator reuse is unaffected by the system-operator cache bypass.

    A second real-operator call still reuses the warm cache (loader called
    once for the real operator); only the interleaved system-operator call
    re-runs the loader.
    """
    real_calls = 0

    async def _counting_loader(_target: SddcTargetLike, operator: Operator) -> dict[str, str]:
        nonlocal real_calls
        if operator.sub != SYSTEM_OPERATOR_SUB:
            real_calls += 1
        return {"username": "svc-meho", "password": "stub-password"}

    connector = SddcManagerConnector(credentials_loader=_counting_loader)
    await connector.auth_headers(_TARGET_A, operator=_make_operator())  # cold real load
    await connector.auth_headers(_TARGET_A, operator=synthesise_system_operator())  # bypass
    await connector.auth_headers(_TARGET_A, operator=_make_operator())  # warm real reuse

    assert real_calls == 1
    await connector.aclose()


@pytest.mark.asyncio
async def test_per_target_isolation_keeps_credentials_separate() -> None:
    """Two targets get two distinct credential cache entries; no cross-target leakage."""
    call_log: list[str] = []

    async def _tracking_loader(target: SddcTargetLike, _operator: Operator) -> dict[str, str]:
        call_log.append(target.name)
        return {"username": f"svc-{target.name}", "password": "pass"}

    connector = SddcManagerConnector(credentials_loader=_tracking_loader)
    h_a = await connector.auth_headers(_TARGET_A, operator=_make_operator())
    h_b = await connector.auth_headers(_TARGET_B, operator=_make_operator())

    username_a, _ = _decode_basic_auth(h_a["Authorization"])
    username_b, _ = _decode_basic_auth(h_b["Authorization"])
    assert username_a == "svc-sddc-a@vsphere.local"
    assert username_b == "svc-sddc-b@vsphere.local"
    # Loader called exactly once per distinct target.
    assert call_log == ["sddc-a", "sddc-b"]
    await connector.aclose()


# ---------------------------------------------------------------------------
# Credential loading failure modes
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_loader_missing_password_key_raises_runtime_error_naming_target() -> None:
    """A loader returning a dict without 'password' surfaces a clear RuntimeError."""

    async def _bad_loader(_target: SddcTargetLike, _operator: Operator) -> dict[str, str]:
        return {"username": "svc-meho"}  # type: ignore[return-value]

    connector = SddcManagerConnector(credentials_loader=_bad_loader)
    with pytest.raises(RuntimeError, match=r"password") as exc_info:
        await connector.auth_headers(_TARGET_A, operator=_make_operator())

    assert "sddc-a" in str(exc_info.value)
    await connector.aclose()


@pytest.mark.asyncio
async def test_loader_missing_username_key_raises_runtime_error_naming_target() -> None:
    """A loader returning a dict without 'username' surfaces a clear RuntimeError."""

    async def _bad_loader(_target: SddcTargetLike, _operator: Operator) -> dict[str, str]:
        return {"password": "stub-password"}  # type: ignore[return-value]

    connector = SddcManagerConnector(credentials_loader=_bad_loader)
    with pytest.raises(RuntimeError, match=r"username") as exc_info:
        await connector.auth_headers(_TARGET_A, operator=_make_operator())

    assert "sddc-a" in str(exc_info.value)
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
        name="sddc-per-user",
        host="sddc.test.invalid",
        port=443,
        secret_ref="sddc/per-user",
        auth_model=auth_model,
    )
    connector = _make_connector()

    with pytest.raises(NotImplementedError) as exc_info:
        await connector.auth_headers(target, operator=_make_operator())

    assert "sddc-per-user" in str(exc_info.value)
    assert auth_model in str(exc_info.value)
    await connector.aclose()


@pytest.mark.asyncio
async def test_auth_headers_accepts_none_auth_model_for_pre_g03_targets() -> None:
    """auth_model=None (pre-G0.3 column-not-yet-populated) is accepted."""
    target = _StubTarget(
        name="sddc-pre-g03",
        host="sddc.test.invalid",
        port=443,
        secret_ref="sddc/pre-g03",
        auth_model=None,
    )
    connector = _make_connector()
    headers = await connector.auth_headers(target, operator=_make_operator())
    assert headers["Authorization"].startswith("Basic ")
    await connector.aclose()


@pytest.mark.asyncio
async def test_auth_headers_accepts_enum_member_for_auth_model() -> None:
    """An AuthModel enum member (not just its string value) is accepted."""
    target = _StubTarget(
        name="sddc-enum",
        host="sddc.test.invalid",
        port=443,
        secret_ref="sddc/enum",
    )
    target.auth_model = AuthModel.SHARED_SERVICE_ACCOUNT  # type: ignore[assignment]
    connector = _make_connector()
    headers = await connector.auth_headers(target, operator=_make_operator())
    assert headers["Authorization"].startswith("Basic ")
    await connector.aclose()


# ---------------------------------------------------------------------------
# fingerprint() + probe()
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fingerprint_canonical_shape_on_reachable_target() -> None:
    """fingerprint() against a respx-mocked GET /v1/sddc-managers returns canonical shape."""
    connector = _make_connector()

    async with respx.mock(base_url="https://sddc-a.test.invalid") as mock:
        mock.get("/v1/sddc-managers").respond(
            200,
            json={
                "elements": [
                    {
                        "id": "sddc-uuid-1",
                        "fqdn": "sddc-a.test.invalid",
                        "version": "9.0.0.0-24276214",
                        "build": "24276214",
                        "domain": {"id": "domain-uuid-1", "name": "MGMT"},
                    }
                ],
                "pageMetadata": {"pageNumber": 1, "pageSize": 10, "totalElements": 1},
            },
        )
        fp = await connector.fingerprint(_TARGET_A)

    assert fp.vendor == "vmware"
    assert fp.product == "sddc-manager"
    assert fp.version == "9.0.0.0-24276214"
    assert fp.build == "24276214"
    assert fp.reachable is True
    assert fp.probe_method == "GET /v1/sddc-managers"
    assert fp.extras["management_domain"] == "MGMT"
    assert fp.extras["management_domain_id"] == "domain-uuid-1"
    assert fp.extras["id"] == "sddc-uuid-1"
    assert fp.extras["fqdn"] == "sddc-a.test.invalid"
    await connector.aclose()


@pytest.mark.asyncio
async def test_fingerprint_unreachable_returns_reachable_false_with_structured_error() -> None:
    """Transport/status failure returns reachable=False with extras['error']."""
    connector = _make_connector()

    async with respx.mock(base_url="https://sddc-a.test.invalid") as mock:
        mock.get("/v1/sddc-managers").respond(401, json={"message": "Unauthorized"})
        fp = await connector.fingerprint(_TARGET_A)

    assert fp.vendor == "vmware"
    assert fp.product == "sddc-manager"
    assert fp.reachable is False
    assert fp.probe_method == "GET /v1/sddc-managers"
    error = fp.extras["error"]
    assert "HTTPStatusError" in error or "401" in error
    await connector.aclose()


@pytest.mark.asyncio
async def test_fingerprint_empty_elements_returns_reachable_true_with_no_version() -> None:
    """An empty elements list produces reachable=True with version=None (API responded)."""
    connector = _make_connector()

    async with respx.mock(base_url="https://sddc-a.test.invalid") as mock:
        mock.get("/v1/sddc-managers").respond(
            200,
            json={"elements": [], "pageMetadata": {"totalElements": 0}},
        )
        fp = await connector.fingerprint(_TARGET_A)

    assert fp.reachable is True
    assert fp.version is None
    assert fp.extras["management_domain"] is None
    await connector.aclose()


@pytest.mark.asyncio
async def test_probe_returns_ok_true_when_reachable() -> None:
    """probe() returns ok=True on a reachable target (delegates to fingerprint)."""
    connector = _make_connector()

    async with respx.mock(base_url="https://sddc-a.test.invalid") as mock:
        mock.get("/v1/sddc-managers").respond(
            200,
            json={
                "elements": [
                    {
                        "id": "u",
                        "fqdn": "sddc-a.test.invalid",
                        "version": "9.0.0.0",
                        "domain": {"id": "d", "name": "MGMT"},
                    }
                ]
            },
        )
        result = await connector.probe(_TARGET_A)

    assert result.ok is True
    assert result.reason is None
    await connector.aclose()


@pytest.mark.asyncio
async def test_probe_returns_ok_false_with_reason_when_unreachable() -> None:
    """probe() returns ok=False + reason on an unreachable target."""
    connector = _make_connector()

    async with respx.mock(base_url="https://sddc-a.test.invalid") as mock:
        mock.get("/v1/sddc-managers").respond(401)
        result = await connector.probe(_TARGET_A)

    assert result.ok is False
    assert result.reason is not None
    await connector.aclose()


# ---------------------------------------------------------------------------
# aclose — credential cache clear + pool tear-down
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_aclose_clears_credential_cache_and_pool() -> None:
    """aclose() clears the in-memory credential cache and tears down the httpx pool."""
    connector = _make_connector()
    await connector.auth_headers(_TARGET_A, operator=_make_operator())
    assert "sddc-a" in connector._creds_cache
    await connector.aclose()
    assert connector._creds_cache == {}
    assert connector._clients == {}


@pytest.mark.asyncio
async def test_aclose_with_no_cached_credentials_is_a_noop() -> None:
    """A fresh connector with no credentials established closes cleanly."""
    connector = _make_connector()
    await connector.aclose()
    assert connector._clients == {}
    assert connector._creds_cache == {}


# ---------------------------------------------------------------------------
# G0.16-T4 (#1306) probe-vs-dispatch convergence
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fingerprint_forwards_route_operator_to_credentials_loader() -> None:
    """G0.16-T4 (#1306) probe-vs-dispatch convergence regression for sddc-manager.

    Pre-#1306 the probe route called ``cls().fingerprint(target)``
    without an operator; the connector synthesised a system operator
    whose placeholder ``raw_jwt`` is not a compact-JWS. Vault's
    JWT/OIDC auth method rejected it before the per-target read,
    surfacing as ``vault OIDC malformed jwt: must have three parts``
    on the v0.8.0 dogfood's ``vcf9-sddc`` probe.

    Post-#1306 the probe route forwards its operator — the same code
    path the dispatch surface uses. Test pins:
    1. The credentials loader receives the route operator.
    2. The forwarded JWT has the compact-JWS shape (≥3 dot-separated
       parts).
    """
    captured: list[Operator] = []

    async def _capturing_loader(
        _target: SddcTargetLike,
        operator: Operator,
    ) -> dict[str, str]:
        captured.append(operator)
        return {"username": "svc-meho", "password": "stub-password"}

    connector = SddcManagerConnector(credentials_loader=_capturing_loader)

    route_operator = Operator(
        sub="op-rdc",
        name="RDC Operator",
        email=None,
        raw_jwt="header.payload.signature",
        tenant_id=UUID("00000000-0000-0000-0000-00000000a0a0"),
        tenant_role=TenantRole.OPERATOR,
    )

    async with respx.mock(base_url="https://sddc-a.test.invalid") as mock:
        mock.get("/v1/sddc-managers").respond(
            200,
            json={
                "elements": [
                    {
                        "id": "sddc-uuid-1",
                        "fqdn": "sddc-a.test.invalid",
                        "version": "9.0.0.0-24276214",
                        "build": "24276214",
                        "domain": {"id": "domain-uuid-1", "name": "MGMT"},
                    }
                ],
                "pageMetadata": {"pageNumber": 1, "pageSize": 10, "totalElements": 1},
            },
        )
        await connector.fingerprint(_TARGET_A, operator=route_operator)

    assert len(captured) == 1
    fwd = captured[0]
    assert fwd.sub == route_operator.sub
    assert len(fwd.raw_jwt.split(".")) >= 3, (
        "forwarded JWT must look like a compact-JWS so Vault accepts it"
    )
    await connector.aclose()


@pytest.mark.asyncio
async def test_fingerprint_without_operator_falls_back_to_system_operator() -> None:
    """``fingerprint(target)`` without ``operator`` synthesises the
    system operator (the system-call carve-out).
    """
    captured: list[Operator] = []

    async def _capturing_loader(
        _target: SddcTargetLike,
        operator: Operator,
    ) -> dict[str, str]:
        captured.append(operator)
        return {"username": "svc-meho", "password": "stub-password"}

    connector = SddcManagerConnector(credentials_loader=_capturing_loader)

    async with respx.mock(base_url="https://sddc-a.test.invalid") as mock:
        mock.get("/v1/sddc-managers").respond(
            200,
            json={"elements": [], "pageMetadata": {}},
        )
        await connector.fingerprint(_TARGET_A)

    assert len(captured) == 1
    assert captured[0].sub == SYSTEM_OPERATOR_SUB
    await connector.aclose()
