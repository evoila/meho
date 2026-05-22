# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Unit tests for GcloudConnector — auth, fingerprint, probe (G3.7-T4 #845).

Exercises:
- ADC + impersonation bearer token applied as Authorization: Bearer.
- SA-JSON-key secret → clear refusal (no token built).
- 401 → token refresh → retry.
- Per-target token isolation.
- fingerprint() against mocked cloudresourcemanager projects.get.
- probe() exercises the impersonation flow; ok=True / ok=False.
- aclose() clears token/creds cache.

Auth approach: a mock ``adc_loader`` replaces ``google.auth.default()``
so tests never touch the real ADC chain. The mock ``credentials_loader``
replaces the Vault stub so the SA-JSON-key-refusal gate is fully
exercisable without a real Vault.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any
from unittest.mock import MagicMock, patch

import httpx
import pytest
import respx

from meho_backplane.connectors.adapters.http import HttpConnector
from meho_backplane.connectors.gcloud import GcloudConnector, GcloudTargetLike
from meho_backplane.connectors.registry import clear_registry, register_connector_v2
from meho_backplane.connectors.schemas import AuthModel

# ---------------------------------------------------------------------------
# Registry hygiene
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clean_gcloud_registry() -> Iterator[None]:
    """Re-register GcloudConnector after sibling tests clear the registry."""
    clear_registry()
    register_connector_v2(
        product=GcloudConnector.product,
        version=GcloudConnector.version,
        impl_id=GcloudConnector.impl_id,
        cls=GcloudConnector,
    )
    yield
    clear_registry()


# ---------------------------------------------------------------------------
# Target stub
# ---------------------------------------------------------------------------


@dataclass
class _StubTarget:
    name: str
    gcp_project: str
    gcp_impersonate_sa: str
    secret_ref: str
    auth_model: str | None = AuthModel.IMPERSONATION.value
    host: str = "gcp.invalid"
    port: int | None = None


_TARGET_A = _StubTarget(
    name="gcloud-a",
    gcp_project="my-project-123",
    gcp_impersonate_sa="svc@my-project-123.iam.gserviceaccount.com",
    secret_ref="kv/data/gcloud/gcloud-a",
)
_TARGET_B = _StubTarget(
    name="gcloud-b",
    gcp_project="other-project-456",
    gcp_impersonate_sa="svc@other-project-456.iam.gserviceaccount.com",
    secret_ref="kv/data/gcloud/gcloud-b",
)


# ---------------------------------------------------------------------------
# Mock ADC loader and credentials
# ---------------------------------------------------------------------------


def _make_mock_creds(token: str = "fake-bearer-token-abc") -> MagicMock:
    """Return a mock that mimics google.auth.impersonated_credentials.Credentials.

    Initial state: token=None (before first refresh), valid=False.
    After refresh(): token=<token>, valid=True — mirrors real impersonated creds
    where token is None until refresh() materialises the bearer string.
    Subsequent refresh() calls simulate a 401-driven re-auth by appending
    "-refreshed" once so the 401-retry test can assert the new token was used.
    """
    mock_creds = MagicMock()
    mock_creds.token = None  # starts unset, as in real impersonated creds
    mock_creds.valid = False
    mock_creds.expired = True
    _refresh_calls = [0]

    def _refresh(request: Any) -> None:
        _refresh_calls[0] += 1
        if _refresh_calls[0] == 1:
            mock_creds.token = token
        else:
            mock_creds.token = token + "-refreshed"
        mock_creds.valid = True
        mock_creds.expired = False

    mock_creds.refresh = _refresh
    return mock_creds


def _make_mock_source_creds() -> MagicMock:
    source = MagicMock()
    source.token = "source-token"
    return source


def _make_adc_loader(
    token: str = "fake-bearer-token-abc",
) -> Any:
    """Return an adc_loader that produces a fixed-token mock credential chain."""
    source_creds = _make_mock_source_creds()
    mock_impersonated = _make_mock_creds(token)

    def _adc_loader(scopes: list[str] | None = None) -> tuple[Any, str | None]:
        return source_creds, "my-project-123"

    def _patch_impersonated(
        source_credentials: Any,
        target_principal: str,
        target_scopes: list[str],
        lifetime: int = 3600,
    ) -> Any:
        return mock_impersonated

    return _adc_loader, _patch_impersonated, mock_impersonated


async def _empty_loader(_target: GcloudTargetLike) -> dict[str, Any]:
    """Return an empty Vault record (no SA key fields — compliant)."""
    return {}


async def _sa_key_loader(_target: GcloudTargetLike) -> dict[str, Any]:
    """Return a Vault record containing SA-JSON-key fields — must be refused."""
    return {
        "type": "service_account",
        "private_key": "-----BEGIN RSA PRIVATE KEY-----\nFAKE\n-----END RSA PRIVATE KEY-----",
        "private_key_id": "key123",
        "client_email": "svc@project.iam.gserviceaccount.com",
        "client_id": "12345",
        "auth_uri": "https://accounts.google.com/o/oauth2/auth",
        "token_uri": "https://oauth2.googleapis.com/token",
    }


def _make_connector(
    token: str = "fake-bearer-token-abc",
    credentials_loader: Any = None,
) -> tuple[GcloudConnector, Any]:
    """Create a GcloudConnector with mocked ADC and credentials_loader.

    Returns ``(connector, mock_impersonated_creds)`` for inspection.
    """
    adc_loader, patch_fn, _mock_creds = _make_adc_loader(token)

    if credentials_loader is None:
        credentials_loader = _empty_loader

    connector = GcloudConnector(
        credentials_loader=credentials_loader,
        adc_loader=adc_loader,
    )

    # Patch impersonated_credentials.Credentials so the synchronous
    # _fetch_token_sync uses our mock without a real ADC chain.
    connector._patch_impersonated = patch_fn  # stored for test assertions
    return connector, _mock_creds


# ---------------------------------------------------------------------------
# ABC + registration plumbing
# ---------------------------------------------------------------------------


def test_gcloud_connector_subclasses_http_connector() -> None:
    assert issubclass(GcloudConnector, HttpConnector)
    assert GcloudConnector.product == "gcloud"
    assert GcloudConnector.version == "v1"
    assert GcloudConnector.impl_id == "gcloud-rest"
    assert GcloudConnector.priority == 1


def test_importing_package_registers_against_v2_registry() -> None:
    from meho_backplane.connectors.registry import all_connectors_v2

    registry = all_connectors_v2()
    key = ("gcloud", "v1", "gcloud-rest")
    assert key in registry
    assert registry[key] is GcloudConnector


def test_default_credentials_loader_raises_until_g03_lands() -> None:
    from meho_backplane.connectors.gcloud.session import load_credentials_from_vault

    async def _check() -> None:
        with pytest.raises(NotImplementedError, match=r"Goal #214"):
            await load_credentials_from_vault(_TARGET_A)

    asyncio.run(_check())


# ---------------------------------------------------------------------------
# Auth model gating
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "auth_model",
    [AuthModel.PER_USER.value, AuthModel.SHARED_SERVICE_ACCOUNT.value, "unknown-mode"],
)
async def test_auth_headers_rejects_non_impersonation_modes(auth_model: str) -> None:
    """Per-user / shared-SA modes raise NotImplementedError naming target + mode."""
    target = _StubTarget(
        name="gcloud-per-user",
        gcp_project="p",
        gcp_impersonate_sa="sa@p.iam.gserviceaccount.com",
        secret_ref="kv/data/gcloud/p",
        auth_model=auth_model,
    )
    connector = GcloudConnector(credentials_loader=_empty_loader)
    with pytest.raises(NotImplementedError) as exc_info:
        await connector.auth_headers(target, raw_jwt="")
    assert "gcloud-per-user" in str(exc_info.value)
    assert auth_model in str(exc_info.value)
    await connector.aclose()


@pytest.mark.asyncio
async def test_auth_headers_accepts_none_auth_model_for_pre_g03_targets() -> None:
    """auth_model=None (pre-G0.3 sentinel) is accepted."""
    target = _StubTarget(
        name="gcloud-pre-g03",
        gcp_project="p",
        gcp_impersonate_sa="sa@p.iam.gserviceaccount.com",
        secret_ref="kv/data/gcloud/p",
        auth_model=None,
    )

    adc_loader, patch_fn, _mc = _make_adc_loader()
    connector = GcloudConnector(credentials_loader=_empty_loader, adc_loader=adc_loader)

    with patch(
        "google.auth.impersonated_credentials.Credentials",
        side_effect=patch_fn,
    ):
        headers = await connector.auth_headers(target, raw_jwt="")

    assert headers["Authorization"].startswith("Bearer ")
    await connector.aclose()


@pytest.mark.asyncio
async def test_auth_headers_accepts_impersonation_enum_member() -> None:
    """AuthModel.IMPERSONATION enum member (not just string value) is accepted."""
    target = _StubTarget(
        name="gcloud-enum",
        gcp_project="p",
        gcp_impersonate_sa="sa@p.iam.gserviceaccount.com",
        secret_ref="kv/data/gcloud/p",
    )
    target.auth_model = AuthModel.IMPERSONATION  # type: ignore[assignment]

    adc_loader, patch_fn, _mc = _make_adc_loader()
    connector = GcloudConnector(credentials_loader=_empty_loader, adc_loader=adc_loader)
    with patch(
        "google.auth.impersonated_credentials.Credentials",
        side_effect=patch_fn,
    ):
        headers = await connector.auth_headers(target, raw_jwt="")
    assert headers["Authorization"].startswith("Bearer ")
    await connector.aclose()


# ---------------------------------------------------------------------------
# SA-JSON-key refusal
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_auth_headers_refuses_sa_json_key_in_secret_ref() -> None:
    """SA-JSON-key fields in the Vault secret → ValueError naming target + fields."""
    connector = GcloudConnector(credentials_loader=_sa_key_loader)
    with pytest.raises(ValueError) as exc_info:
        await connector.auth_headers(_TARGET_A, raw_jwt="")
    msg = str(exc_info.value)
    assert "gcloud-a" in msg
    assert "private_key" in msg
    assert "disableServiceAccountKeyCreation" in msg
    # No token should have been built
    assert _TARGET_A.name not in connector._token_cache
    await connector.aclose()


@pytest.mark.asyncio
async def test_auth_headers_sa_key_refusal_does_not_leak_token() -> None:
    """After a refusal, the token cache stays empty — no partial state."""
    connector = GcloudConnector(credentials_loader=_sa_key_loader)
    with contextlib.suppress(ValueError):
        await connector.auth_headers(_TARGET_A, raw_jwt="")
    assert not connector._token_cache
    assert not connector._creds_cache
    await connector.aclose()


# ---------------------------------------------------------------------------
# ADC + impersonation bearer token
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_auth_headers_applies_bearer_token_from_impersonation() -> None:
    """auth_headers() applies the impersonated bearer token as Authorization: Bearer."""
    adc_loader, patch_fn, _mc = _make_adc_loader("tok-xyz")
    connector = GcloudConnector(credentials_loader=_empty_loader, adc_loader=adc_loader)

    with patch(
        "google.auth.impersonated_credentials.Credentials",
        side_effect=patch_fn,
    ):
        headers = await connector.auth_headers(_TARGET_A, raw_jwt="")

    assert headers == {"Authorization": "Bearer tok-xyz"}
    await connector.aclose()


@pytest.mark.asyncio
async def test_auth_headers_caches_token_across_calls() -> None:
    """Second auth_headers call against same target reuses cached token."""
    fetch_count = 0

    def _counting_adc(scopes: list[str] | None = None) -> tuple[Any, str | None]:
        nonlocal fetch_count
        fetch_count += 1
        return _make_mock_source_creds(), "p"

    mock_creds = _make_mock_creds("tok-cached")
    connector = GcloudConnector(credentials_loader=_empty_loader, adc_loader=_counting_adc)

    with patch(
        "google.auth.impersonated_credentials.Credentials",
        return_value=mock_creds,
    ):
        h1 = await connector.auth_headers(_TARGET_A, raw_jwt="")
        h2 = await connector.auth_headers(_TARGET_A, raw_jwt="")

    assert h1 == h2
    assert fetch_count == 1  # ADC only called once
    await connector.aclose()


# ---------------------------------------------------------------------------
# Per-target token isolation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_per_target_token_isolation() -> None:
    """Two targets get two distinct tokens; no cross-target leakage."""
    tokens: dict[str, str] = {
        "gcloud-a": "token-for-a",
        "gcloud-b": "token-for-b",
    }

    def _adc_for_target_a(scopes: list[str] | None = None) -> tuple[Any, str | None]:
        return _make_mock_source_creds(), "p-a"

    creds_a = _make_mock_creds(tokens["gcloud-a"])
    creds_b = _make_mock_creds(tokens["gcloud-b"])
    creds_seq = [creds_a, creds_b]
    call_idx = 0

    def _patch_impersonated(
        source_credentials: Any,
        target_principal: str,
        target_scopes: list[str],
        lifetime: int = 3600,
    ) -> Any:
        nonlocal call_idx
        c = creds_seq[call_idx]
        call_idx += 1
        return c

    connector = GcloudConnector(
        credentials_loader=_empty_loader,
        adc_loader=_adc_for_target_a,
    )

    with patch(
        "google.auth.impersonated_credentials.Credentials",
        side_effect=_patch_impersonated,
    ):
        h_a = await connector.auth_headers(_TARGET_A, raw_jwt="")
        h_b = await connector.auth_headers(_TARGET_B, raw_jwt="")

    assert h_a["Authorization"] == f"Bearer {tokens['gcloud-a']}"
    assert h_b["Authorization"] == f"Bearer {tokens['gcloud-b']}"
    await connector.aclose()


# ---------------------------------------------------------------------------
# 401 → token refresh → retry
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_401_triggers_token_refresh_and_retry() -> None:
    """A 401 from the GCP API triggers one token refresh and a retry."""
    mock_creds = _make_mock_creds("initial-token")

    def _adc(scopes: list[str] | None = None) -> tuple[Any, str | None]:
        return _make_mock_source_creds(), "p"

    connector = GcloudConnector(credentials_loader=_empty_loader, adc_loader=_adc)
    url = "https://cloudresourcemanager.googleapis.com/v1/projects/my-project-123"

    with (
        patch("google.auth.impersonated_credentials.Credentials", return_value=mock_creds),
        patch("google.auth.transport.requests.Request"),
        respx.mock() as mock,
    ):
        # Pre-populate the token cache so the first call uses "initial-token"
        await connector.auth_headers(_TARGET_A, raw_jwt="")
        assert connector._token_cache.get("gcloud-a") == "initial-token"

        # First call: 401; second call: 200 (after token refresh)
        call_count = 0
        route = mock.get(url)

        def _side_effect(request: Any) -> httpx.Response:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return httpx.Response(401, json={"error": "unauthorized"})
            return httpx.Response(200, json={"projectId": "my-project-123"})

        route.side_effect = _side_effect

        result = await connector._get_json_abs(_TARGET_A, url)

    assert result == {"projectId": "my-project-123"}
    assert call_count == 2  # initial + retry after refresh
    await connector.aclose()


# ---------------------------------------------------------------------------
# fingerprint()
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fingerprint_canonical_shape_on_reachable_target() -> None:
    """fingerprint() returns canonical shape from cloudresourcemanager.projects.get."""
    mock_creds = _make_mock_creds("tok")
    adc_loader, _pf, _ = _make_adc_loader("tok")
    connector = GcloudConnector(credentials_loader=_empty_loader, adc_loader=adc_loader)

    with (
        patch(
            "google.auth.impersonated_credentials.Credentials",
            return_value=mock_creds,
        ),
        respx.mock() as mock,
    ):
        mock.get("https://cloudresourcemanager.googleapis.com/v1/projects/my-project-123").respond(
            200,
            json={
                "projectId": "my-project-123",
                "projectNumber": "987654321",
                "lifecycleState": "ACTIVE",
                "parent": {"type": "organization", "id": "112233445566"},
            },
        )
        fp = await connector.fingerprint(_TARGET_A)

    assert fp.vendor == "google"
    assert fp.product == "gcp-project"
    assert fp.version is None
    assert fp.reachable is True
    assert fp.probe_method == "GET cloudresourcemanager.googleapis.com/v1/projects"
    assert fp.extras["project_number"] == "987654321"
    assert fp.extras["lifecycle_state"] == "ACTIVE"
    assert fp.extras["organization"] == "112233445566"
    assert fp.extras["project_id"] == "my-project-123"
    await connector.aclose()


@pytest.mark.asyncio
async def test_fingerprint_unreachable_returns_reachable_false() -> None:
    """Transport failure returns reachable=False with extras['error']."""
    mock_creds = _make_mock_creds("tok")
    adc_loader, _pf, _ = _make_adc_loader("tok")
    connector = GcloudConnector(credentials_loader=_empty_loader, adc_loader=adc_loader)

    with (
        patch(
            "google.auth.impersonated_credentials.Credentials",
            return_value=mock_creds,
        ),
        respx.mock() as mock,
    ):
        mock.get("https://cloudresourcemanager.googleapis.com/v1/projects/my-project-123").respond(
            403, json={"error": {"status": "PERMISSION_DENIED"}}
        )
        fp = await connector.fingerprint(_TARGET_A)

    assert fp.vendor == "google"
    assert fp.product == "gcp-project"
    assert fp.reachable is False
    assert "error" in fp.extras
    assert "403" in fp.extras["error"] or "HTTPStatusError" in fp.extras["error"]
    await connector.aclose()


@pytest.mark.asyncio
async def test_fingerprint_no_parent_org_leaves_organization_none() -> None:
    """When parent.type != 'organization', extras['organization'] is None."""
    mock_creds = _make_mock_creds("tok")
    adc_loader, _pf, _ = _make_adc_loader("tok")
    connector = GcloudConnector(credentials_loader=_empty_loader, adc_loader=adc_loader)

    with (
        patch(
            "google.auth.impersonated_credentials.Credentials",
            return_value=mock_creds,
        ),
        respx.mock() as mock,
    ):
        mock.get("https://cloudresourcemanager.googleapis.com/v1/projects/my-project-123").respond(
            200,
            json={
                "projectId": "my-project-123",
                "projectNumber": "111",
                "lifecycleState": "ACTIVE",
                "parent": {"type": "folder", "id": "folder-42"},
            },
        )
        fp = await connector.fingerprint(_TARGET_A)

    assert fp.extras["organization"] is None
    await connector.aclose()


# ---------------------------------------------------------------------------
# probe()
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_probe_returns_ok_true_on_successful_impersonation_flow() -> None:
    """probe() returns ok=True when CRM returns 200 with matching projectId."""
    mock_creds = _make_mock_creds("tok")
    adc_loader, _pf, _ = _make_adc_loader("tok")
    connector = GcloudConnector(credentials_loader=_empty_loader, adc_loader=adc_loader)

    with (
        patch(
            "google.auth.impersonated_credentials.Credentials",
            return_value=mock_creds,
        ),
        respx.mock() as mock,
    ):
        mock.get("https://cloudresourcemanager.googleapis.com/v1/projects/my-project-123").respond(
            200, json={"projectId": "my-project-123", "lifecycleState": "ACTIVE"}
        )
        result = await connector.probe(_TARGET_A)

    assert result.ok is True
    assert result.reason is None
    await connector.aclose()


@pytest.mark.asyncio
async def test_probe_returns_ok_false_on_transport_error() -> None:
    """probe() returns ok=False + reason on transport failure."""
    mock_creds = _make_mock_creds("tok")
    adc_loader, _pf, _ = _make_adc_loader("tok")
    connector = GcloudConnector(credentials_loader=_empty_loader, adc_loader=adc_loader)

    with (
        patch(
            "google.auth.impersonated_credentials.Credentials",
            return_value=mock_creds,
        ),
        respx.mock() as mock,
    ):
        mock.get("https://cloudresourcemanager.googleapis.com/v1/projects/my-project-123").respond(
            403
        )
        result = await connector.probe(_TARGET_A)

    assert result.ok is False
    assert result.reason is not None
    await connector.aclose()


@pytest.mark.asyncio
async def test_probe_returns_ok_false_on_project_id_mismatch() -> None:
    """probe() returns ok=False when projectId in response doesn't match target."""
    mock_creds = _make_mock_creds("tok")
    adc_loader, _pf, _ = _make_adc_loader("tok")
    connector = GcloudConnector(credentials_loader=_empty_loader, adc_loader=adc_loader)

    with (
        patch(
            "google.auth.impersonated_credentials.Credentials",
            return_value=mock_creds,
        ),
        respx.mock() as mock,
    ):
        mock.get("https://cloudresourcemanager.googleapis.com/v1/projects/my-project-123").respond(
            200, json={"projectId": "WRONG-PROJECT", "lifecycleState": "ACTIVE"}
        )
        result = await connector.probe(_TARGET_A)

    assert result.ok is False
    assert result.reason is not None
    assert "mismatch" in result.reason
    await connector.aclose()


# ---------------------------------------------------------------------------
# aclose
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_aclose_clears_token_and_creds_cache() -> None:
    """aclose() clears in-memory token and credentials caches."""
    mock_creds = _make_mock_creds("tok")
    adc_loader, _pf, _ = _make_adc_loader("tok")
    connector = GcloudConnector(credentials_loader=_empty_loader, adc_loader=adc_loader)

    with patch(
        "google.auth.impersonated_credentials.Credentials",
        return_value=mock_creds,
    ):
        await connector.auth_headers(_TARGET_A, raw_jwt="")

    assert "gcloud-a" in connector._token_cache
    await connector.aclose()
    assert connector._token_cache == {}
    assert connector._creds_cache == {}
    assert connector._clients == {}


@pytest.mark.asyncio
async def test_aclose_with_empty_cache_is_noop() -> None:
    """A fresh connector with nothing cached closes cleanly."""
    connector = GcloudConnector(credentials_loader=_empty_loader)
    await connector.aclose()
    assert connector._token_cache == {}
    assert connector._clients == {}
