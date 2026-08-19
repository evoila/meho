# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Unit tests for :class:`InstallerConnector` auth + fingerprint/probe + status poll.

Exercises the ``session_login_token`` flow (the same scheme SDDC Manager uses):
the connector POSTs ``{username, password}`` to ``/v1/tokens``, reads
``accessToken`` out of the ``TokenPair`` response, and sends it as
``Authorization: Bearer <accessToken>`` on every subsequent request. Covers
cached-token reuse, the auth_model boundary gate, a login failure, the
fingerprint/probe shape against a mocked ``GET /v1/system/appliance-info``, the
bring-up status poll, and cache tear-down. All respx-mocked — no network.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import dataclass, field
from uuid import UUID

import pytest
import respx

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors._shared.cache_key import target_cache_key
from meho_backplane.connectors._shared.vcf_auth import SessionLoginError
from meho_backplane.connectors.adapters.http import HttpConnector
from meho_backplane.connectors.registry import clear_registry, register_connector_v2
from meho_backplane.connectors.schemas import AuthModel
from meho_backplane.connectors.vcf_installer import InstallerConnector, InstallerTargetLike

_TOKEN_PATH = "/v1/tokens"
_APPLIANCE_INFO_PATH = "/v1/system/appliance-info"


def _make_operator(raw_jwt: str = "") -> Operator:
    return Operator(
        sub="test-operator",
        name=None,
        email=None,
        raw_jwt=raw_jwt,
        tenant_id=UUID(int=0),
        tenant_role=TenantRole.OPERATOR,
    )


@pytest.fixture(autouse=True)
def _clean_installer_registry() -> Iterator[None]:
    """Re-register InstallerConnector after sibling tests clear the registry."""
    clear_registry()
    register_connector_v2(
        product=InstallerConnector.product,
        version=InstallerConnector.version,
        impl_id=InstallerConnector.impl_id,
        cls=InstallerConnector,
    )
    yield
    clear_registry()


@dataclass
class _StubTarget:
    name: str
    host: str
    port: int | None
    secret_ref: str
    auth_model: str | None = AuthModel.SHARED_SERVICE_ACCOUNT.value
    tls_server_name: str | None = None
    id: UUID = field(default_factory=lambda: UUID(int=1))
    tenant_id: UUID = field(default_factory=lambda: UUID(int=0))


_TARGET = _StubTarget(
    name="installer-a",
    host="installer-a.test.invalid",
    port=443,
    secret_ref="installer/installer-a",
)


async def _stub_loader(_target: InstallerTargetLike, _operator: Operator) -> dict[str, str]:
    return {"username": "admin@local", "password": "stub-password"}


def _make_connector() -> InstallerConnector:
    return InstallerConnector(credentials_loader=_stub_loader)


# --------------------------------------------------------------- plumbing


def test_installer_connector_subclasses_http_connector() -> None:
    assert issubclass(InstallerConnector, HttpConnector)
    assert InstallerConnector.product == "installer"
    assert InstallerConnector.version == "9.1"
    assert InstallerConnector.impl_id == "installer-rest"
    assert InstallerConnector.supported_version_range == ">=9.1,<10.0"
    assert InstallerConnector.priority == 1


def test_importing_package_registers_against_v2_registry() -> None:
    from meho_backplane.connectors.registry import all_connectors_v2

    registry = all_connectors_v2()
    assert registry[("installer", "9.1", "installer-rest")] is InstallerConnector


# --------------------------------------------------------------- auth


@pytest.mark.asyncio
async def test_auth_headers_mints_and_sends_bearer_token() -> None:
    connector = _make_connector()
    async with respx.mock(base_url="https://installer-a.test.invalid") as mock:
        token_route = mock.post(_TOKEN_PATH).respond(
            201, json={"accessToken": "tok-abc", "refreshToken": {"id": "r"}}
        )
        headers = await connector.auth_headers(_TARGET, operator=_make_operator())

    assert headers == {"Authorization": "Bearer tok-abc"}
    assert token_route.call_count == 1
    login_request = token_route.calls[0].request
    assert login_request.headers.get("authorization") is None  # no HTTP Basic
    assert json.loads(login_request.content) == {
        "username": "admin@local",
        "password": "stub-password",
    }
    await connector.aclose()


@pytest.mark.asyncio
async def test_auth_headers_reuses_cached_token() -> None:
    calls = {"n": 0}

    async def _counting_loader(_t: InstallerTargetLike, _o: Operator) -> dict[str, str]:
        calls["n"] += 1
        return {"username": "admin@local", "password": "stub-password"}

    connector = InstallerConnector(credentials_loader=_counting_loader)
    async with respx.mock(base_url="https://installer-a.test.invalid") as mock:
        token_route = mock.post(_TOKEN_PATH).respond(201, json={"accessToken": "tok-abc"})
        h1 = await connector.auth_headers(_TARGET, operator=_make_operator())
        h2 = await connector.auth_headers(_TARGET, operator=_make_operator())

    assert h1 == h2 == {"Authorization": "Bearer tok-abc"}
    assert calls["n"] == 1
    assert token_route.call_count == 1
    await connector.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "auth_model", [AuthModel.PER_USER.value, AuthModel.IMPERSONATION.value, "unknown-mode"]
)
async def test_auth_headers_rejects_non_shared_service_account(auth_model: str) -> None:
    target = _StubTarget(
        name="installer-per-user",
        host="installer.test.invalid",
        port=443,
        secret_ref="installer/per-user",
        auth_model=auth_model,
    )
    connector = _make_connector()
    with pytest.raises(NotImplementedError) as exc_info:
        await connector.auth_headers(target, operator=_make_operator())
    assert "installer-per-user" in str(exc_info.value)
    assert auth_model in str(exc_info.value)
    await connector.aclose()


@pytest.mark.asyncio
async def test_login_non_2xx_raises_session_login_error_naming_target() -> None:
    connector = _make_connector()
    async with respx.mock(base_url="https://installer-a.test.invalid") as mock:
        mock.post(_TOKEN_PATH).respond(401, json={"message": "bad credentials"})
        with pytest.raises(SessionLoginError, match=r"installer-a"):
            await connector.auth_headers(_TARGET, operator=_make_operator())
    await connector.aclose()


# --------------------------------------------------------------- fingerprint


@pytest.mark.asyncio
async def test_fingerprint_canonical_shape_on_reachable_target() -> None:
    connector = _make_connector()
    async with respx.mock(base_url="https://installer-a.test.invalid") as mock:
        mock.post(_TOKEN_PATH).respond(201, json={"accessToken": "tok-abc"})
        info_route = mock.get(_APPLIANCE_INFO_PATH).respond(
            200,
            json={"role": "INSTALLER", "version": "9.1.0.0", "dnsDomain": "vcf.lab.local"},
        )
        fp = await connector.fingerprint(_TARGET)

    assert fp.vendor == "vmware"
    assert fp.product == "installer"
    assert fp.version == "9.1.0.0"
    assert fp.reachable is True
    assert fp.probe_method == "GET /v1/system/appliance-info"
    assert fp.extras["role"] == "INSTALLER"
    assert fp.extras["dns_domain"] == "vcf.lab.local"
    # The appliance-info GET carried the minted Bearer token — never HTTP Basic.
    assert info_route.calls[0].request.headers.get("authorization") == "Bearer tok-abc"
    await connector.aclose()


@pytest.mark.asyncio
async def test_fingerprint_unreachable_on_persistent_401() -> None:
    connector = _make_connector()
    async with respx.mock(base_url="https://installer-a.test.invalid") as mock:
        mock.post(_TOKEN_PATH).respond(201, json={"accessToken": "tok-abc"})
        mock.get(_APPLIANCE_INFO_PATH).respond(401, json={"message": "Unauthorized"})
        fp = await connector.fingerprint(_TARGET)

    assert fp.reachable is False
    assert fp.probe_method == "GET /v1/system/appliance-info"
    assert "401" in fp.extras["error"]
    await connector.aclose()


@pytest.mark.asyncio
async def test_probe_returns_ok_true_when_reachable() -> None:
    connector = _make_connector()
    async with respx.mock(base_url="https://installer-a.test.invalid") as mock:
        mock.post(_TOKEN_PATH).respond(201, json={"accessToken": "tok-abc"})
        mock.get(_APPLIANCE_INFO_PATH).respond(
            200, json={"role": "INSTALLER", "version": "9.1.0.0"}
        )
        result = await connector.probe(_TARGET)
    assert result.ok is True
    assert result.reason is None
    await connector.aclose()


# --------------------------------------------------------------- status poll


@pytest.mark.asyncio
async def test_sddc_status_reads_the_bringup_task() -> None:
    connector = _make_connector()
    async with respx.mock(base_url="https://installer-a.test.invalid") as mock:
        mock.post(_TOKEN_PATH).respond(201, json={"accessToken": "tok-abc"})
        status_route = mock.get("/v1/sddcs/sddc-1").respond(
            200,
            json={
                "id": "sddc-1",
                "name": "bringup-mgmt",
                "status": "IN_PROGRESS",
                "vcfInstanceName": "vcf-mgmt-01",
                "sddcSubTasks": [],
            },
        )
        result = await connector.sddc_status(_make_operator(), _TARGET, {"id": "sddc-1"})

    assert result["status"] == "IN_PROGRESS"
    assert result["id"] == "sddc-1"
    assert status_route.calls[0].request.headers.get("authorization") == "Bearer tok-abc"
    await connector.aclose()


# --------------------------------------------------------------- aclose


@pytest.mark.asyncio
async def test_aclose_clears_caches_and_pool() -> None:
    connector = _make_connector()
    async with respx.mock(base_url="https://installer-a.test.invalid") as mock:
        mock.post(_TOKEN_PATH).respond(201, json={"accessToken": "tok-abc"})
        await connector.auth_headers(_TARGET, operator=_make_operator())
    assert target_cache_key(_TARGET) in connector._creds_cache
    assert target_cache_key(_TARGET) in connector._session_tokens
    await connector.aclose()
    assert connector._creds_cache == {}
    assert connector._session_tokens == {}
    assert connector._clients == {}
