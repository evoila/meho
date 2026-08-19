# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Unit tests for :class:`Vrli8Connector` — the legacy vRLI 8.x thin subclass (#3068).

``Vrli8Connector`` subclasses
:class:`~meho_backplane.connectors.vcf_logs.connector.VcfLogsConnector` (the 9.x
sibling) and overrides **only** the registration triple and the version band. The
vRLI ``/api/v2/*`` surface — including the ``Bearer`` session-auth scheme — is
identical on 8.x and 9.x (the v2 API shipped in vRLI 8.0), so there is **no auth
override**. These tests pin the 8.x metadata/registration and prove the inherited
auth + fingerprint still work on the subclass:

* ``auth_headers`` mints via ``POST /api/v2/sessions`` and presents ``Bearer <sessionId>``.
* The inherited ``auth_model`` gate still fires.
* ``fingerprint`` reads the unauthenticated ``GET /api/v2/version`` and reports a real 8.x version.

Models :mod:`tests.test_connectors_vcf_logs_auth` (the 9.x sibling).
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from uuid import UUID, uuid4

import pytest
import respx

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors._shared.vcf_auth import VcfTargetLike
from meho_backplane.connectors.adapters.http import HttpConnector
from meho_backplane.connectors.registry import (
    all_connectors_v2,
    clear_registry,
    register_connector_v2,
)
from meho_backplane.connectors.schemas import AuthModel
from meho_backplane.connectors.vrli8 import (
    VRLI8_IMPL_ID,
    VRLI8_PRODUCT,
    VRLI8_VERSION,
)
from meho_backplane.connectors.vrli8.connector import Vrli8Connector

_SESSIONS_PATH = "/api/v2/sessions"
_VERSION_PATH = "/api/v2/version"
_SESSION_ID = "vrli8-session-abc-123"


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
    """Restore Vrli8Connector's package registration (versioned triple only, no wildcard)."""
    clear_registry()
    register_connector_v2(
        product=Vrli8Connector.product,
        version=Vrli8Connector.version,
        impl_id=Vrli8Connector.impl_id,
        cls=Vrli8Connector,
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
    provider: str | None = None
    tls_server_name: str | None = None
    id: UUID = field(default_factory=uuid4)
    tenant_id: UUID = field(default_factory=lambda: UUID(int=0))


_TARGET_A = _StubTarget(name="vrli8-a", host="vrli8-a.test.invalid", port=443, secret_ref="vrli/a")


async def _stub_loader(_target: VcfTargetLike, _operator: Operator) -> dict[str, str]:
    return {"username": "svc-meho", "password": "stub-password"}


def _make_connector() -> Vrli8Connector:
    return Vrli8Connector(credentials_loader=_stub_loader)


# ---------------------------------------------------------------------------
# Subclass metadata + registration
# ---------------------------------------------------------------------------


def test_vrli8_connector_subclasses_the_modern_impl() -> None:
    """The connector inherits the 9.x impl (and thus HttpConnector) with 8.x metadata."""
    from meho_backplane.connectors.vcf_logs.connector import VcfLogsConnector

    assert issubclass(Vrli8Connector, VcfLogsConnector)
    assert issubclass(Vrli8Connector, HttpConnector)
    assert Vrli8Connector.product == "vrli"
    assert Vrli8Connector.version == "8.0"
    assert Vrli8Connector.impl_id == "vrli-vrli8"
    assert Vrli8Connector.supported_version_range == ">=8.0,<9.0"
    assert Vrli8Connector.priority == 1


def test_importing_package_registers_versioned_triple_only() -> None:
    """The package registers its versioned triple and NOT the ``("vrli","","")`` wildcard."""
    registry = all_connectors_v2()
    assert registry[(VRLI8_PRODUCT, VRLI8_VERSION, VRLI8_IMPL_ID)] is Vrli8Connector
    assert (VRLI8_PRODUCT, "", "") not in registry


# ---------------------------------------------------------------------------
# Inherited Bearer auth (unchanged from 9.x)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_auth_headers_mints_session_and_presents_bearer() -> None:
    """``auth_headers`` POSTs creds to /api/v2/sessions and presents ``Bearer <sessionId>``."""
    connector = _make_connector()
    async with respx.mock(base_url="https://vrli8-a.test.invalid") as mock:
        session = mock.post(_SESSIONS_PATH).respond(
            200, json={"sessionId": _SESSION_ID, "ttl": 1800}
        )
        headers = await connector.auth_headers(_TARGET_A, operator=_make_operator())
    assert session.called
    assert headers == {"Authorization": f"Bearer {_SESSION_ID}"}
    await connector.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "auth_model",
    [AuthModel.PER_USER.value, AuthModel.IMPERSONATION.value, "unknown-mode"],
)
async def test_inherited_auth_model_guard_still_rejects_non_shared_modes(auth_model: str) -> None:
    """The base-class ``auth_model`` gate fires unchanged through the subclass."""
    target = _StubTarget(
        name="vrli8-per-user",
        host="vrli8.test.invalid",
        port=443,
        secret_ref="vrli/pu",
        auth_model=auth_model,
    )
    connector = _make_connector()
    with pytest.raises(NotImplementedError) as exc_info:
        await connector.auth_headers(target, operator=_make_operator())
    assert "vrli8-per-user" in str(exc_info.value)
    assert auth_model in str(exc_info.value)
    await connector.aclose()


# ---------------------------------------------------------------------------
# fingerprint — inherited unauthenticated GET /api/v2/version, real 8.x version
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fingerprint_reports_real_8x_version_unauthenticated() -> None:
    """``GET /api/v2/version`` (no session) → reachable, product=vrli, a parsed 8.x version."""
    connector = _make_connector()
    async with respx.mock(base_url="https://vrli8-a.test.invalid") as mock:
        version = mock.get(_VERSION_PATH).respond(200, json={"version": "8.18.0.0.24842179"})
        fp = await connector.fingerprint(_TARGET_A, operator=_make_operator())
    assert version.called
    assert fp.vendor == "vmware"
    assert fp.product == "vrli"
    assert fp.reachable is True
    assert fp.version is not None and fp.version.startswith("8.18")
    assert fp.build == "24842179"
    await connector.aclose()
