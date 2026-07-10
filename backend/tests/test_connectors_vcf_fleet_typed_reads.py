# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for the VCF Fleet typed read core (T4 · #2304, Initiative #2266).

Covers the audited Fleet read set converted from ingested-row curation to
typed ops on the connector's existing HTTP Basic (LCM-local) session:

* ``fleet.about`` — the about/health probe.
* ``fleet.environment.list`` — the component inventory ("what's deployed").

Coverage matrix (per #2304 acceptance criteria):

* **Both ops dispatch as ``source_kind="typed"`` on a fresh boot with
  zero catalog state** — the persisted ``endpoint_descriptor`` rows carry
  ``source_kind="typed"`` after ``register_operations`` runs against an
  empty DB, and each dispatches end-to-end through
  :func:`~meho_backplane.operations.dispatch` against a respx-mocked
  appliance, returning ``status="ok"`` with the payload in
  ``OperationResult.result``. No ingested row is involved.
* **``fleet.environment.list`` wraps the bare Fleet array** under an
  ``environments`` key (the vSphere typed-reads envelope shape).
* **``fleet.about`` carries the 9.0 HTTP-500 fallback guidance** in its
  ``llm_instructions`` (the regression warning that moved off the
  ingested curation with the op).
* **Registration shape** — both are ``safety_level="safe"``,
  ``requires_approval=False``, read-only. No write op is registered.

Mirrors :mod:`tests.test_connectors_argocd_reads` for the typed dispatch
lifecycle; injects a stub credentials loader on the resolved connector
instance (the :mod:`tests.test_connectors_vcf_fleet_e2e` pattern) so no
Vault read fires — Fleet auth is HTTP Basic, not a bearer token.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Iterator
from typing import Any
from unittest.mock import AsyncMock
from uuid import UUID

import pytest
import respx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors.registry import clear_registry, register_connector_v2
from meho_backplane.connectors.vcf_fleet import (
    FLEET_CONNECTOR_ID,
    FLEET_TYPED_OPS,
    VcfFleetConnector,
)
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import EndpointDescriptor
from meho_backplane.operations import dispatch, reset_dispatcher_caches
from meho_backplane.operations._handler_resolve import (
    get_or_create_connector_instance,
    reset_handler_cache,
)
from meho_backplane.settings import get_settings

_PRODUCT = "fleet"
_VERSION = "9.0"
_IMPL_ID = "fleet-rest"

_FLEET_HOST = "fleet-typed.test.invalid"
_FLEET_BASE_URL = f"https://{_FLEET_HOST}"


@pytest.fixture(autouse=True)
def _settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Pin chassis env vars Settings reads (Operator + dispatcher)."""
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _reset_module_state() -> Iterator[None]:
    """Reset dispatcher/handler caches + connector registry around every test."""
    reset_dispatcher_caches()
    reset_handler_cache()
    clear_registry()
    register_connector_v2(
        product=_PRODUCT,
        version=_VERSION,
        impl_id=_IMPL_ID,
        cls=VcfFleetConnector,
    )
    yield
    reset_dispatcher_caches()
    reset_handler_cache()
    clear_registry()


@pytest.fixture
def _stub_embedding(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    """Deterministic embedding stub so registration doesn't pull ONNX."""
    service = AsyncMock()
    service.encode_one.return_value = [0.1] * 384
    service.encode.return_value = [[0.1] * 384]
    service.dimension = 384
    monkeypatch.setattr(
        "meho_backplane.operations.typed_register.encode_endpoint_text",
        AsyncMock(return_value=[0.1] * 384),
    )
    return service


@pytest.fixture
async def session() -> AsyncIterator[AsyncSession]:
    """AsyncSession against the autouse-migrated per-worker SQLite engine."""
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as s:
        yield s


class _FleetTarget:
    """Target satisfying both ``VcfFleetTargetLike`` and the resolver shape."""

    def __init__(self) -> None:
        self.product = _PRODUCT
        self.fingerprint = type("_FP", (), {"version": _VERSION})()
        self.preferred_impl_id: str | None = None
        self.id: UUID = uuid.uuid4()
        self.tenant_id: UUID = uuid.UUID("00000000-0000-0000-0000-0000000000fe")
        self.name = "fleet-typed"
        self.host = _FLEET_HOST
        self.port = 443
        self.secret_ref = "targets/fleet/fleet-typed"
        self.auth_model = "shared_service_account"


def _make_operator() -> Operator:
    """Operator carrying a non-empty raw_jwt (the fail-closed gate passes)."""
    return Operator(
        sub="op-typed-fleet",
        name="Fleet Typed Reads Operator",
        email=None,
        raw_jwt="op.typed.fleet.jwt",
        tenant_id=UUID("00000000-0000-0000-0000-0000000000ab"),
        tenant_role=TenantRole.OPERATOR,
    )


async def _fleet_stub_loader(_target: object, _operator: Operator) -> dict[str, str]:
    """Stub HTTP Basic credentials — bypasses the Vault read."""
    return {"username": "admin@local", "password": "fleet-typed-pw"}


async def _register_and_bind() -> VcfFleetConnector:
    """Register the typed ops + resolve the connector with a stub creds loader.

    Resolving the instance via :func:`get_or_create_connector_instance`
    before dispatch populates the dispatcher's instance cache, so the
    stub-loader injection lands on the same instance the dispatch reuses.
    """
    await VcfFleetConnector.register_operations()
    instance = get_or_create_connector_instance(VcfFleetConnector)
    instance._creds = type(instance._creds)(  # type: ignore[attr-defined]
        _fleet_stub_loader,
        product_label="fleet",
    )
    return instance


_ABOUT_PAYLOAD: dict[str, Any] = {
    "apiVersion": "8.0",
    "productVersion": "9.0.0.0",
    "buildNumber": "24123456",
    "releaseDate": "2026-04-01",
}
_ENVIRONMENTS_PAYLOAD: list[dict[str, Any]] = [
    {
        "environmentId": "env-typed-000",
        "environmentName": "env-typed-000",
        "environmentStatus": "DEPLOY_SUCCESSFUL",
        "products": [{"productId": "vrops", "version": "9.0.0"}],
    },
    {
        "environmentId": "env-typed-001",
        "environmentName": "env-typed-001",
        "environmentStatus": "DEPLOY_SUCCESSFUL",
        "products": [{"productId": "vrli", "version": "9.0.0"}],
    },
]


# ---------------------------------------------------------------------------
# AC #1 — typed dispatch on a fresh boot with zero catalog state
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_about_dispatches_typed_on_fresh_boot(
    _stub_embedding: AsyncMock,
    session: AsyncSession,
) -> None:
    """fleet.about dispatches as source_kind='typed' against a mocked appliance."""
    instance = await _register_and_bind()
    try:
        target = _FleetTarget()
        operator = _make_operator()
        async with respx.mock(base_url=_FLEET_BASE_URL, assert_all_called=False) as mock:
            route = mock.get("/lcm/lcops/api/v2/about").respond(200, json=_ABOUT_PAYLOAD)
            result = await dispatch(
                operator=operator,
                connector_id=FLEET_CONNECTOR_ID,
                op_id="fleet.about",
                target=target,
                params={},
            )
        assert result.status == "ok", result.error
        assert result.result == _ABOUT_PAYLOAD
        assert route.called and route.call_count == 1
        sent_auth = route.calls[0].request.headers.get("authorization")
        assert sent_auth is not None and sent_auth.startswith("Basic ")
    finally:
        await instance.aclose()

    descriptor = (
        await session.execute(
            select(EndpointDescriptor).where(EndpointDescriptor.op_id == "fleet.about")
        )
    ).scalar_one()
    assert descriptor.source_kind == "typed"
    assert descriptor.tenant_id is None


@pytest.mark.asyncio
async def test_environment_list_dispatches_typed_and_wraps_array(
    _stub_embedding: AsyncMock,
    session: AsyncSession,
) -> None:
    """fleet.environment.list dispatches typed and wraps the bare array."""
    instance = await _register_and_bind()
    try:
        target = _FleetTarget()
        operator = _make_operator()
        async with respx.mock(base_url=_FLEET_BASE_URL, assert_all_called=False) as mock:
            route = mock.get("/lcm/lcops/api/v2/environments").respond(
                200, json=_ENVIRONMENTS_PAYLOAD
            )
            result = await dispatch(
                operator=operator,
                connector_id=FLEET_CONNECTOR_ID,
                op_id="fleet.environment.list",
                target=target,
                params={},
            )
        assert result.status == "ok", result.error
        assert result.result == {"environments": _ENVIRONMENTS_PAYLOAD}
        assert route.called and route.call_count == 1
    finally:
        await instance.aclose()

    descriptor = (
        await session.execute(
            select(EndpointDescriptor).where(EndpointDescriptor.op_id == "fleet.environment.list")
        )
    ).scalar_one()
    assert descriptor.source_kind == "typed"


@pytest.mark.asyncio
async def test_typed_ops_registered_with_no_ingested_rows(
    _stub_embedding: AsyncMock,
    session: AsyncSession,
) -> None:
    """A fresh boot registers exactly the typed ops — no ingested descriptors.

    Proves the "zero catalog state" half of AC #1: after
    ``register_operations`` runs against an empty DB, the only Fleet
    ``endpoint_descriptor`` rows are the typed ops (no spec was ingested).
    """
    await VcfFleetConnector.register_operations()

    rows = (
        (
            await session.execute(
                select(EndpointDescriptor).where(EndpointDescriptor.product == _PRODUCT)
            )
        )
        .scalars()
        .all()
    )
    op_ids = {r.op_id for r in rows}
    assert op_ids == {"fleet.about", "fleet.environment.list"}
    assert all(r.source_kind == "typed" for r in rows)


# ---------------------------------------------------------------------------
# Registration shape
# ---------------------------------------------------------------------------


def test_typed_ops_are_read_only_and_ungated() -> None:
    """Both typed ops are safe, read-only, and require no approval."""
    assert {op.op_id for op in FLEET_TYPED_OPS} == {
        "fleet.about",
        "fleet.environment.list",
    }
    for op in FLEET_TYPED_OPS:
        assert op.safety_level == "safe", op.op_id
        assert op.requires_approval is False, op.op_id
        assert "read-only" in op.tags, op.op_id


def test_about_llm_instructions_flag_the_9_0_regression() -> None:
    """fleet.about's llm_instructions warn about the VCF 9.0 HTTP 500 + fallback."""
    about = next(op for op in FLEET_TYPED_OPS if op.op_id == "fleet.about")
    assert about.llm_instructions is not None
    combined = " ".join(str(v) for v in about.llm_instructions.values()).lower()
    assert "500" in combined, about.llm_instructions
    assert "datacenter" in combined, about.llm_instructions
