# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Unit tests for :class:`Vrops8Connector` — the legacy vROps 8.x scheme swap (#3067).

``Vrops8Connector`` is a thin subclass of
:class:`~meho_backplane.connectors.vcf_operations.connector.VcfOperationsConnector`
(the 9.x sibling). The only behavioural delta is the token *scheme*: an 8.x
appliance predates the ``OpsToken`` alias (#2395) and accepts only the pre-9.x
``vRealizeOpsToken``. These tests pin exactly that delta — and prove the parts that
are **inherited** (the ``POST /suite-api/api/auth/token/acquire`` mint, the
``auth_model`` gate, ``invalidate_session`` recovery, the ``versions/current``
fingerprint) still work and now present the 8.x scheme end-to-end:

* ``auth_headers`` returns ``Authorization: vRealizeOpsToken <token>`` (not ``OpsToken``).
* A dispatched read + the fingerprint both carry ``vRealizeOpsToken`` on the wire.
* The inherited auth-model guard, per-target isolation, and eviction hooks are unbroken.

Models :mod:`tests.test_connectors_vcf_operations_auth` (the 9.x sibling) with the
8.x divergence.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
import respx

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors._shared.cache_key import target_cache_key
from meho_backplane.connectors._shared.vault_creds import VaultCredentialsReadError
from meho_backplane.connectors._shared.vcf_auth import VcfTargetLike
from meho_backplane.connectors.adapters.http import HttpConnector
from meho_backplane.connectors.registry import (
    all_connectors_v2,
    clear_registry,
    register_connector_v2,
)
from meho_backplane.connectors.schemas import AuthModel
from meho_backplane.connectors.vrops8 import (
    VROPS8_IMPL_ID,
    VROPS8_PRODUCT,
    VROPS8_VERSION,
)
from meho_backplane.connectors.vrops8.connector import Vrops8Connector

_ACQUIRE_PATH = "/suite-api/api/auth/token/acquire"
_VERSIONS_PATH = "/suite-api/api/versions/current"
_OPS_TOKEN = "vrops8-ops-token-abc-123"


def _acquire_response(token: str = _OPS_TOKEN) -> dict[str, Any]:
    """A canonical ``token/acquire`` 200 body carrying *token*."""
    return {"token": token, "validity": 1470421325035, "expiresAt": "later", "roles": []}


def _make_operator(raw_jwt: str = "op.test.jwt") -> Operator:
    return Operator(
        sub="test-operator",
        name=None,
        email=None,
        raw_jwt=raw_jwt,
        tenant_id=UUID(int=0),
        tenant_role=TenantRole.OPERATOR,
    )


@pytest.fixture(autouse=True)
def _clean_registry() -> Iterator[None]:
    """Restore Vrops8Connector's package registration (versioned triple only, no wildcard)."""
    clear_registry()
    register_connector_v2(
        product=Vrops8Connector.product,
        version=Vrops8Connector.version,
        impl_id=Vrops8Connector.impl_id,
        cls=Vrops8Connector,
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
    auth_source: str | None = None
    tls_server_name: str | None = None
    id: UUID = field(default_factory=uuid4)
    tenant_id: UUID = field(default_factory=lambda: UUID(int=0))


_TARGET_A = _StubTarget(
    name="vrops8-a", host="vrops8-a.test.invalid", port=443, secret_ref="vrops/a"
)


async def _stub_loader(_target: VcfTargetLike, _operator: Operator) -> dict[str, str]:
    return {"username": "admin", "password": "stub-password"}


def _make_connector() -> Vrops8Connector:
    return Vrops8Connector(credentials_loader=_stub_loader)


# ---------------------------------------------------------------------------
# Subclass metadata + registration
# ---------------------------------------------------------------------------


def test_vrops8_connector_subclasses_the_modern_impl() -> None:
    """The connector inherits the 9.x impl (and thus HttpConnector) with 8.x metadata."""
    from meho_backplane.connectors.vcf_operations.connector import VcfOperationsConnector

    assert issubclass(Vrops8Connector, VcfOperationsConnector)
    assert issubclass(Vrops8Connector, HttpConnector)
    assert Vrops8Connector.product == "vrops"
    assert Vrops8Connector.version == "8.0"
    assert Vrops8Connector.impl_id == "vrops-vrops8"
    assert Vrops8Connector.supported_version_range == ">=8.0,<9.0"
    assert Vrops8Connector.priority == 1


def test_importing_package_registers_versioned_triple_only() -> None:
    """The package registers its versioned triple and NOT the ``("vrops","","")`` wildcard."""
    registry = all_connectors_v2()
    assert registry[(VROPS8_PRODUCT, VROPS8_VERSION, VROPS8_IMPL_ID)] is Vrops8Connector
    assert (VROPS8_PRODUCT, "", "") not in registry


# ---------------------------------------------------------------------------
# The scheme swap — the one behavioural delta
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_auth_headers_presents_vrealizeopstoken_scheme() -> None:
    """``auth_headers`` mints via acquire and presents the pre-9.x ``vRealizeOpsToken`` scheme."""
    connector = _make_connector()
    async with respx.mock(base_url="https://vrops8-a.test.invalid") as mock:
        mock.post(_ACQUIRE_PATH).respond(200, json=_acquire_response())
        headers = await connector.auth_headers(_TARGET_A, operator=_make_operator())
    # The delta from the 9.x sibling: the scheme is vRealizeOpsToken, not the bare
    # OpsToken (note vRealizeOpsToken *contains* the substring "OpsToken" — assert the
    # exact scheme token, not a substring).
    assert headers == {"Authorization": f"vRealizeOpsToken {_OPS_TOKEN}"}
    assert headers["Authorization"].split(" ", 1)[0] == "vRealizeOpsToken" != "OpsToken"
    await connector.aclose()


@pytest.mark.asyncio
async def test_dispatched_read_carries_the_8x_scheme_on_the_wire() -> None:
    """A data read mints once and sends ``vRealizeOpsToken`` on the DATA request (not acquire)."""
    connector = _make_connector()
    async with respx.mock(base_url="https://vrops8-a.test.invalid") as mock:
        mock.post(_ACQUIRE_PATH).respond(200, json=_acquire_response())
        versions = mock.get(_VERSIONS_PATH).respond(
            200, json={"releaseName": "8.18.0.12345", "buildNumber": 12345}
        )
        await connector._get_json(_TARGET_A, _VERSIONS_PATH, operator=_make_operator())
    data_req = versions.calls[0].request
    assert data_req.headers["Authorization"] == f"vRealizeOpsToken {_OPS_TOKEN}"
    await connector.aclose()


# ---------------------------------------------------------------------------
# Inherited behaviour still holds (single-sourced on the base)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "auth_model",
    [AuthModel.PER_USER.value, AuthModel.IMPERSONATION.value, "unknown-mode"],
)
async def test_inherited_auth_model_guard_still_rejects_non_shared_modes(auth_model: str) -> None:
    """The base-class ``auth_model`` gate fires unchanged through the subclass override.

    Proves the security-sensitive guard is single-sourced on the base: the override
    calls ``super().auth_headers`` (which runs the gate) before swapping the scheme.
    """
    target = _StubTarget(
        name="vrops8-per-user",
        host="vrops8.test.invalid",
        port=443,
        secret_ref="vrops/pu",
        auth_model=auth_model,
    )
    connector = _make_connector()
    with pytest.raises(NotImplementedError) as exc_info:
        await connector.auth_headers(target, operator=_make_operator())
    assert "vrops8-per-user" in str(exc_info.value)
    assert auth_model in str(exc_info.value)
    await connector.aclose()


@pytest.mark.asyncio
async def test_session_establish_rejects_empty_operator_jwt() -> None:
    """A system-initiated call (empty raw_jwt) fails closed before any mint (inherited)."""
    connector = _make_connector()
    with pytest.raises(VaultCredentialsReadError, match=r"vrops8-a"):
        await connector.auth_headers(_TARGET_A, operator=_make_operator(raw_jwt=""))
    await connector.aclose()


@pytest.mark.asyncio
async def test_token_cached_then_evicted_by_invalidate_session_remints() -> None:
    """The inherited token cache + ``invalidate_session`` (#2067) evict-then-remint works."""
    connector = _make_connector()
    async with respx.mock(base_url="https://vrops8-a.test.invalid") as mock:
        acquire = mock.post(_ACQUIRE_PATH).mock(
            side_effect=[
                httpx.Response(200, json=_acquire_response("tok-1")),
                httpx.Response(200, json=_acquire_response("tok-2")),
            ]
        )
        h1 = await connector.auth_headers(_TARGET_A, operator=_make_operator())
        assert connector._session_tokens == {target_cache_key(_TARGET_A): "tok-1"}
        await connector.invalidate_session(_TARGET_A)
        assert connector._session_tokens == {}
        h2 = await connector.auth_headers(_TARGET_A, operator=_make_operator())
    assert h1 == {"Authorization": "vRealizeOpsToken tok-1"}
    assert h2 == {"Authorization": "vRealizeOpsToken tok-2"}
    assert acquire.call_count == 2
    await connector.aclose()


@pytest.mark.asyncio
async def test_authsource_rides_the_acquire_body_inherited() -> None:
    """A target with ``auth_source`` set threads it onto the acquire body (inherited behaviour)."""
    target = _StubTarget(
        name="vrops8-ad",
        host="vrops8-ad.test.invalid",
        port=443,
        secret_ref="vrops/ad",
        auth_source="corp-ad",
    )
    connector = _make_connector()
    async with respx.mock(base_url="https://vrops8-ad.test.invalid") as mock:
        acquire = mock.post(_ACQUIRE_PATH).respond(200, json=_acquire_response())
        await connector.auth_headers(target, operator=_make_operator())
    body = acquire.calls[0].request.content
    assert b"corp-ad" in body
    await connector.aclose()


# ---------------------------------------------------------------------------
# fingerprint — inherited, reports the real 8.x version, uses the 8.x scheme
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fingerprint_reports_real_8x_version_and_uses_8x_scheme() -> None:
    """``versions/current`` → product=vrops + 8.x releaseName; authed via vRealizeOpsToken."""
    connector = _make_connector()
    async with respx.mock(base_url="https://vrops8-a.test.invalid") as mock:
        mock.post(_ACQUIRE_PATH).respond(200, json=_acquire_response())
        versions = mock.get(_VERSIONS_PATH).respond(
            200, json={"releaseName": "8.18.0.12345", "buildNumber": 12345}
        )
        fp = await connector.fingerprint(_TARGET_A, operator=_make_operator())
    assert fp.vendor == "vmware"
    assert fp.product == "vrops"
    assert fp.reachable is True
    assert fp.version == "8.18.0.12345"
    assert fp.build == "12345"
    # The fingerprint's authenticated GET presents the 8.x scheme too.
    assert versions.calls[0].request.headers["Authorization"] == f"vRealizeOpsToken {_OPS_TOKEN}"
    await connector.aclose()
