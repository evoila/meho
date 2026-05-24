# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Unit tests for :class:`VcfOperationsConnector` (G3.6-T1 #829).

Exercises HTTP Basic auth, the optional ``auth-source`` query parameter,
per-target credential isolation, the auth_model boundary gate, and the
fingerprint/probe shapes against mocked vROps ``/suite-api/api/*`` endpoints.

Auth: no session token — HTTP Basic sent on every request. Credentials
cached per target so Vault is only queried once per target per connector
instance lifetime. The ``auth-source`` query parameter rides on every
authenticated request when ``target.auth_source`` is set; absent when unset.
"""

from __future__ import annotations

import asyncio
import base64
from collections.abc import Iterator
from dataclasses import dataclass
from uuid import UUID

import pytest
import respx

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors._shared.vcf_auth import VcfTargetLike
from meho_backplane.connectors.adapters.http import HttpConnector
from meho_backplane.connectors.registry import (
    clear_registry,
    register_connector_v2,
)
from meho_backplane.connectors.schemas import AuthModel
from meho_backplane.connectors.vcf_operations import (
    VcfOperationsConnector,
    VcfOperationsTargetLike,
)


def _make_operator(raw_jwt: str = "op.test.jwt") -> Operator:
    """Return a minimal :class:`Operator` for threading through the auth surface.

    Defaults ``raw_jwt`` to a non-empty placeholder so the cache fast-path's
    defense-in-depth fail-closed guard (``VaultCredentialsReadError`` on
    empty ``operator.raw_jwt``, mirroring the loader-path guard) doesn't
    fire on tests that don't care about the value. Tests that exercise the
    empty-jwt rejection pass ``raw_jwt=""`` explicitly.
    """
    return Operator(
        sub="test-operator",
        name=None,
        email=None,
        raw_jwt=raw_jwt,
        tenant_id=UUID(int=0),
        tenant_role=TenantRole.OPERATOR,
    )


@pytest.fixture(autouse=True)
def _clean_vcf_operations_registry() -> Iterator[None]:
    """Re-register VcfOperationsConnector after sibling tests clear the registry.

    ``test_connectors_registry_v2.py`` installs an autouse fixture that calls
    :func:`clear_registry` between tests. Re-register before every test in
    this module and clear after — same pattern
    :mod:`tests.test_connectors_harbor_auth` established.
    """
    clear_registry()
    register_connector_v2(
        product=VcfOperationsConnector.product,
        version=VcfOperationsConnector.version,
        impl_id=VcfOperationsConnector.impl_id,
        cls=VcfOperationsConnector,
    )
    yield
    clear_registry()


# ---------------------------------------------------------------------------
# Target stub — satisfies VcfOperationsTargetLike Protocol structurally.
# Replaced by the real Target model when G0.3 (#224) lands ``auth_source``.
# ---------------------------------------------------------------------------


@dataclass
class _StubTarget:
    name: str
    host: str
    port: int | None
    secret_ref: str
    auth_model: str | None = AuthModel.SHARED_SERVICE_ACCOUNT.value
    auth_source: str | None = None


_TARGET_A = _StubTarget(
    name="vrops-a",
    host="vrops-a.test.invalid",
    port=443,
    secret_ref="kv/data/vrops/vrops-a",
)
_TARGET_B = _StubTarget(
    name="vrops-b",
    host="vrops-b.test.invalid",
    port=443,
    secret_ref="kv/data/vrops/vrops-b",
)
_TARGET_WITH_AUTH_SOURCE = _StubTarget(
    name="vrops-ad",
    host="vrops-ad.test.invalid",
    port=443,
    secret_ref="kv/data/vrops/vrops-ad",
    auth_source="corp-ad",
)


async def _stub_loader(_target: VcfTargetLike, _operator: Operator) -> dict[str, str]:
    """Return canned local-account credentials regardless of the target."""
    return {"username": "admin", "password": "stub-password"}


def _make_connector() -> VcfOperationsConnector:
    return VcfOperationsConnector(credentials_loader=_stub_loader)


def _decode_basic_auth(authorization_header: str) -> tuple[str, str]:
    """Decode an ``Authorization: Basic <b64>`` header into (username, password)."""
    assert authorization_header.startswith("Basic ")
    decoded = base64.b64decode(authorization_header[6:]).decode()
    username, _, password = decoded.partition(":")
    return username, password


# ---------------------------------------------------------------------------
# ABC + registration plumbing
# ---------------------------------------------------------------------------


def test_vcf_operations_connector_subclasses_http_connector() -> None:
    assert issubclass(VcfOperationsConnector, HttpConnector)
    assert VcfOperationsConnector.product == "vcf-operations"
    assert VcfOperationsConnector.version == "9.0"
    assert VcfOperationsConnector.impl_id == "vrops-rest"
    assert VcfOperationsConnector.supported_version_range == ">=9.0,<10.0"
    assert VcfOperationsConnector.priority == 1


def test_importing_package_registers_against_v2_registry() -> None:
    from meho_backplane.connectors.registry import all_connectors_v2

    registry = all_connectors_v2()
    key = ("vcf-operations", "9.0", "vrops-rest")
    assert key in registry
    assert registry[key] is VcfOperationsConnector


def test_default_credentials_loader_fails_closed_without_operator_jwt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The default loader is the live shared operator-context Vault read (G3.10-T2).

    Empty ``raw_jwt`` is fail-closed — system-initiated calls have no
    operator JWT to forward to Vault's JWT/OIDC auth method, so the
    helper raises :class:`VaultCredentialsReadError` rather than
    silently falling back to a backplane identity. End-to-end coverage
    of the wired read lives in ``test_connectors_vcf_operations_credread.py``.
    """
    from meho_backplane.connectors._shared.vault_creds import VaultCredentialsReadError
    from meho_backplane.connectors.vcf_operations.session import (
        load_credentials_from_vault,
    )
    from meho_backplane.settings import get_settings

    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    monkeypatch.setenv("VAULT_OIDC_ROLE", "meho-mcp")
    monkeypatch.setenv("VAULT_OIDC_MOUNT_PATH", "jwt")
    monkeypatch.setenv("VAULT_TIMEOUT_SECONDS", "5.0")
    monkeypatch.delenv("VAULT_NAMESPACE", raising=False)
    get_settings.cache_clear()

    async def _check() -> None:
        with pytest.raises(VaultCredentialsReadError, match=r"vrops-a"):
            await load_credentials_from_vault(_TARGET_A, _make_operator(raw_jwt=""))

    try:
        asyncio.run(_check())
    finally:
        get_settings.cache_clear()


# ---------------------------------------------------------------------------
# HTTP Basic auth header
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_auth_headers_sends_basic_auth() -> None:
    """auth_headers() produces Authorization: Basic with stub credentials."""
    connector = _make_connector()
    headers = await connector.auth_headers(_TARGET_A, operator=_make_operator())

    assert "Authorization" in headers
    assert headers["Authorization"].startswith("Basic ")
    username, password = _decode_basic_auth(headers["Authorization"])
    assert username == "admin"
    assert password == "stub-password"
    await connector.aclose()


# ---------------------------------------------------------------------------
# Credential caching
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_auth_headers_reuses_cached_credentials_across_calls() -> None:
    """Second auth_headers call against the same target does NOT re-invoke the loader."""
    call_count = 0

    async def _counting_loader(_target: VcfTargetLike, _operator: Operator) -> dict[str, str]:
        nonlocal call_count
        call_count += 1
        return {"username": "admin", "password": "stub-password"}

    connector = VcfOperationsConnector(credentials_loader=_counting_loader)
    h1 = await connector.auth_headers(_TARGET_A, operator=_make_operator())
    h2 = await connector.auth_headers(_TARGET_A, operator=_make_operator())

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

    async def _tracking_loader(target: VcfTargetLike, _operator: Operator) -> dict[str, str]:
        call_log.append(target.name)
        return {"username": f"svc-{target.name}", "password": "pass"}

    connector = VcfOperationsConnector(credentials_loader=_tracking_loader)
    h_a = await connector.auth_headers(_TARGET_A, operator=_make_operator())
    h_b = await connector.auth_headers(_TARGET_B, operator=_make_operator())

    username_a, _ = _decode_basic_auth(h_a["Authorization"])
    username_b, _ = _decode_basic_auth(h_b["Authorization"])
    assert username_a == "svc-vrops-a"
    assert username_b == "svc-vrops-b"
    assert call_log == ["vrops-a", "vrops-b"]
    await connector.aclose()


# ---------------------------------------------------------------------------
# Credential loading failure modes
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_loader_missing_password_key_raises_runtime_error_naming_target() -> None:
    async def _bad_loader(_target: VcfTargetLike, _operator: Operator) -> dict[str, str]:
        return {"username": "admin"}

    connector = VcfOperationsConnector(credentials_loader=_bad_loader)
    with pytest.raises(RuntimeError, match=r"password") as exc_info:
        await connector.auth_headers(_TARGET_A, operator=_make_operator())

    assert "vrops-a" in str(exc_info.value)
    await connector.aclose()


@pytest.mark.asyncio
async def test_loader_missing_username_key_raises_runtime_error_naming_target() -> None:
    async def _bad_loader(_target: VcfTargetLike, _operator: Operator) -> dict[str, str]:
        return {"password": "stub-password"}

    connector = VcfOperationsConnector(credentials_loader=_bad_loader)
    with pytest.raises(RuntimeError, match=r"username") as exc_info:
        await connector.auth_headers(_TARGET_A, operator=_make_operator())

    assert "vrops-a" in str(exc_info.value)
    await connector.aclose()


# ---------------------------------------------------------------------------
# Auth model gating
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "auth_model",
    [AuthModel.PER_USER.value, AuthModel.IMPERSONATION.value, "unknown-mode"],
)
async def test_auth_headers_rejects_non_shared_service_account_modes(
    auth_model: str,
) -> None:
    """Per-user / impersonation modes raise NotImplementedError naming the target + mode."""
    target = _StubTarget(
        name="vrops-per-user",
        host="vrops.test.invalid",
        port=443,
        secret_ref="kv/data/vrops/per-user",
        auth_model=auth_model,
    )
    connector = _make_connector()

    with pytest.raises(NotImplementedError) as exc_info:
        await connector.auth_headers(target, operator=_make_operator())

    assert "vrops-per-user" in str(exc_info.value)
    assert auth_model in str(exc_info.value)
    await connector.aclose()


@pytest.mark.asyncio
async def test_auth_headers_accepts_none_auth_model_for_pre_g03_targets() -> None:
    """auth_model=None (pre-G0.3 column-not-yet-populated) is accepted."""
    target = _StubTarget(
        name="vrops-pre-g03",
        host="vrops.test.invalid",
        port=443,
        secret_ref="kv/data/vrops/pre-g03",
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
        name="vrops-enum",
        host="vrops.test.invalid",
        port=443,
        secret_ref="kv/data/vrops/enum",
    )
    target.auth_model = AuthModel.SHARED_SERVICE_ACCOUNT  # type: ignore[assignment]
    connector = _make_connector()
    headers = await connector.auth_headers(target, operator=_make_operator())
    assert headers["Authorization"].startswith("Basic ")
    await connector.aclose()


# ---------------------------------------------------------------------------
# auth-source query parameter (vROps-specific)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_auth_source_appended_as_query_param_when_set() -> None:
    """An authenticated request against a target with auth_source carries ?auth-source=..."""
    connector = _make_connector()

    async with respx.mock(base_url="https://vrops-ad.test.invalid") as mock:
        route = mock.get("/suite-api/api/versions/current").respond(
            200,
            json={"releaseName": "9.0.0", "buildNumber": 12345678},
        )
        await connector.fingerprint(_TARGET_WITH_AUTH_SOURCE)

    assert route.called
    sent_url = route.calls[0].request.url
    assert sent_url.params.get("auth-source") == "corp-ad"
    await connector.aclose()


@pytest.mark.asyncio
async def test_auth_source_omitted_from_query_when_unset() -> None:
    """Targets without auth_source send no auth-source query parameter."""
    connector = _make_connector()

    async with respx.mock(base_url="https://vrops-a.test.invalid") as mock:
        route = mock.get("/suite-api/api/versions/current").respond(
            200,
            json={"releaseName": "9.0.0", "buildNumber": 12345678},
        )
        await connector.fingerprint(_TARGET_A)

    assert route.called
    sent_url = route.calls[0].request.url
    assert "auth-source" not in sent_url.params
    await connector.aclose()


@pytest.mark.asyncio
async def test_auth_source_empty_string_is_treated_as_unset() -> None:
    """``auth_source=""`` is silent-omitted — vROps rejects ?auth-source= empty values."""
    target = _StubTarget(
        name="vrops-empty-source",
        host="vrops-empty-source.test.invalid",
        port=443,
        secret_ref="kv/data/vrops/empty-source",
        auth_source="",
    )
    connector = _make_connector()

    async with respx.mock(base_url="https://vrops-empty-source.test.invalid") as mock:
        route = mock.get("/suite-api/api/versions/current").respond(
            200,
            json={"releaseName": "9.0.0", "buildNumber": 12345678},
        )
        await connector.fingerprint(target)

    assert route.called
    sent_url = route.calls[0].request.url
    assert "auth-source" not in sent_url.params
    await connector.aclose()


# ---------------------------------------------------------------------------
# fingerprint()
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fingerprint_canonical_shape_on_reachable_target() -> None:
    """fingerprint() against mocked GET /suite-api/api/versions/current returns canonical shape."""
    connector = _make_connector()

    async with respx.mock(base_url="https://vrops-a.test.invalid") as mock:
        mock.get("/suite-api/api/versions/current").respond(
            200,
            json={
                "releaseName": "9.0.0",
                "buildNumber": 23456789,
                "humanlyReadableReleaseName": "VMware Aria Operations 9.0",
            },
        )
        fp = await connector.fingerprint(_TARGET_A)

    assert fp.vendor == "vmware"
    assert fp.product == "vcf-operations"
    assert fp.version == "9.0.0"
    assert fp.build == "23456789"
    assert fp.reachable is True
    assert fp.probe_method == "GET /suite-api/api/versions/current"
    assert fp.extras["humanly_readable_release_name"] == "VMware Aria Operations 9.0"
    await connector.aclose()


@pytest.mark.asyncio
async def test_fingerprint_without_humanly_readable_name() -> None:
    """A vROps response missing humanlyReadableReleaseName leaves the extras key None."""
    connector = _make_connector()

    async with respx.mock(base_url="https://vrops-a.test.invalid") as mock:
        mock.get("/suite-api/api/versions/current").respond(
            200,
            json={"releaseName": "9.0.0", "buildNumber": 23456789},
        )
        fp = await connector.fingerprint(_TARGET_A)

    assert fp.version == "9.0.0"
    assert fp.build == "23456789"
    assert fp.reachable is True
    assert fp.extras["humanly_readable_release_name"] is None
    await connector.aclose()


@pytest.mark.asyncio
async def test_fingerprint_unreachable_returns_reachable_false_with_structured_error() -> None:
    """Transport/status failure returns reachable=False with extras['error']."""
    connector = _make_connector()

    async with respx.mock(base_url="https://vrops-a.test.invalid") as mock:
        mock.get("/suite-api/api/versions/current").respond(
            401,
            json={"error": "unauthorised"},
        )
        fp = await connector.fingerprint(_TARGET_A)

    assert fp.vendor == "vmware"
    assert fp.product == "vcf-operations"
    assert fp.reachable is False
    assert fp.probe_method == "GET /suite-api/api/versions/current"
    error = fp.extras["error"]
    assert "HTTPStatusError" in error or "401" in error
    await connector.aclose()


# ---------------------------------------------------------------------------
# probe()
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_probe_returns_ok_true_on_reachable_target() -> None:
    """probe() returns ok=True when the version endpoint is reachable (delegates to fingerprint)."""
    connector = _make_connector()

    async with respx.mock(base_url="https://vrops-a.test.invalid") as mock:
        mock.get("/suite-api/api/versions/current").respond(
            200,
            json={"releaseName": "9.0.0", "buildNumber": 23456789},
        )
        result = await connector.probe(_TARGET_A)

    assert result.ok is True
    assert result.reason is None
    await connector.aclose()


@pytest.mark.asyncio
async def test_probe_returns_ok_false_with_reason_on_transport_error() -> None:
    """probe() returns ok=False + reason on transport/status failure."""
    connector = _make_connector()

    async with respx.mock(base_url="https://vrops-a.test.invalid") as mock:
        # 401 is non-retryable (4xx) — surfaces immediately on the
        # fingerprint call which probe() delegates to.
        mock.get("/suite-api/api/versions/current").respond(401)
        result = await connector.probe(_TARGET_A)

    assert result.ok is False
    assert result.reason is not None
    assert "HTTPStatusError" in result.reason or "401" in result.reason
    await connector.aclose()


# ---------------------------------------------------------------------------
# aclose
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_aclose_clears_credential_cache_and_pool() -> None:
    """aclose() clears the in-memory credential cache and tears down the httpx pool."""
    connector = _make_connector()
    await connector.auth_headers(_TARGET_A, operator=_make_operator())
    assert "vrops-a" in connector._creds.cached_targets
    await connector.aclose()
    assert connector._creds.cached_targets == frozenset()
    assert connector._clients == {}


@pytest.mark.asyncio
async def test_aclose_with_no_cached_credentials_is_a_noop() -> None:
    """A fresh connector with no credentials established closes cleanly."""
    connector = _make_connector()
    await connector.aclose()
    assert connector._clients == {}
    assert connector._creds.cached_targets == frozenset()


# ---------------------------------------------------------------------------
# Protocol satisfaction
# ---------------------------------------------------------------------------


def test_stub_target_satisfies_protocol_at_runtime() -> None:
    """_StubTarget satisfies the runtime-checkable VcfOperationsTargetLike Protocol.

    Guards against drift between the Protocol shape and the test fixture: a
    new required field on the Protocol that the stub doesn't carry would fail
    this check before the rest of the suite produces confusing AttributeErrors.
    """
    assert isinstance(_TARGET_A, VcfOperationsTargetLike)
    assert isinstance(_TARGET_WITH_AUTH_SOURCE, VcfOperationsTargetLike)
