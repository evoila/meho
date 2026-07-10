# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Typed VCFA read ops on the dual-plane session (T5 #2305).

Covers
:mod:`meho_backplane.connectors.vcf_automation.typed_ops` and the
connector handlers + registrar that back it. VCFA ships no vendor spec,
so these ops are ``source_kind="typed"`` — they dispatch through the
connector's own dual-plane session with **zero catalog / ingested
state**. Acceptance contract (#2305):

* **Typed dispatch on a fresh boot with zero catalog state** — after
  ``register_typed_operations()`` and with **no** ingested
  ``endpoint_descriptor`` rows seeded, all five audited ops (org list,
  region list, provider health, tenant project list, tenant about)
  dispatch through ``call_operation`` against a respx-mocked VCFA
  appliance and return ``status="ok"``, with ``source_kind="typed"`` on
  every registered row.

* **Each op declares which plane it rides, and plane selection is
  tested** — every op's declared ``plane`` matches
  ``plane_for_path(op.path)`` (static invariant), and a live provider-op
  dispatch establishes the **provider** session (Basic →
  ``X-VMWARE-VCLOUD-ACCESS-TOKEN`` JWT, ``Accept:
  application/json;version=9.0.0``) while a live tenant-op dispatch
  establishes the **tenant** session (JSON-body login → ``{"token": …}``,
  ``Accept: application/json``). Cross-plane token isolation is asserted:
  a provider dispatch never populates the tenant cache and vice versa.

The respx routes + inlined payloads mirror the recorded-fixture shape of
:mod:`tests.test_connectors_vcf_automation_e2e`; the dual-plane refresh
tool intentionally excludes VCFA (G3.6-T13 #841), so the payloads live
inline next to the assertions.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock
from uuid import UUID

import httpx
import pytest
import respx
from sqlalchemy import select

import meho_backplane.operations._audit as audit_module
from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors._shared.cache_key import target_cache_key
from meho_backplane.connectors.registry import all_connectors_v2
from meho_backplane.connectors.schemas import FingerprintResult
from meho_backplane.connectors.vcf_automation import (
    VCFA_CONNECTOR_ID,
    VCFA_IMPL_ID,
    VCFA_PRODUCT,
    VCFA_TYPED_OPS,
    VCFA_VERSION,
    VcfAutomationConnector,
)
from meho_backplane.connectors.vcf_automation._routing import (
    PROVIDER_CLOUDAPI_ACCEPT,
    TENANT_ACCEPT,
    plane_for_path,
)
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import EndpointDescriptor, Target
from meho_backplane.operations import reset_dispatcher_caches
from meho_backplane.operations._handler_resolve import (
    get_or_create_connector_instance,
    reset_handler_cache,
)
from meho_backplane.operations.meta_tools import call_operation

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_OPERATOR_TENANT: UUID = UUID("00000000-0000-0000-0000-0000000000fb")
_FQDN = "vcfa-typed.test.invalid"
_BASE_URL = f"https://{_FQDN}"
_TARGET_NAME = "vcfa-typed-target"

_PROVIDER_JWT = "vcfa-typed-provider-jwt"
_TENANT_TOKEN = "vcfa-typed-tenant-token"

_OPERATOR = Operator(
    sub="vcfa-typed-test",
    name="VCFA Typed Test Operator",
    email=None,
    raw_jwt="<vcfa-typed-raw-jwt>",
    tenant_id=_OPERATOR_TENANT,
    tenant_role=TenantRole.TENANT_ADMIN,
)

# The connector class registers under the target product slug ("vcfa"),
# which is also what parse_connector_id("vcfa-rest-9.0") derives — so the
# typed descriptor triple and the registry key coincide for VCFA.
_REGISTRY_PRODUCT = VcfAutomationConnector.product

_FINGERPRINT: dict[str, Any] = FingerprintResult(
    vendor="vmware",
    product="vcfa",
    version="9.0",
    reachable=True,
    probed_at=datetime(2026, 7, 10, 10, 0, 0, tzinfo=UTC),
    probe_method="GET /api/versions + GET /iaas/api/about",
    extras={"planes": ["provider", "tenant"]},
).model_dump(mode="json")


# ---------------------------------------------------------------------------
# Inlined respx payloads (recorded-fixture shape)
# ---------------------------------------------------------------------------

_PROVIDER_ORGS: dict[str, Any] = {
    "values": [{"id": "org-1", "name": "acme", "displayName": "Acme", "isEnabled": True}],
    "resultTotal": 1,
}
_PROVIDER_REGIONS: dict[str, Any] = {
    "values": [{"id": "region-1", "name": "rg-a", "nsxManager": "nsx-1", "isEnabled": True}],
    "resultTotal": 1,
}
_PROVIDER_SITE: dict[str, Any] = {
    "id": "site-1",
    "name": "VCFA-TYPED",
    "restName": "vcfa-typed-rest",
    "productVersion": "9.0.0.0-12345",
}
_TENANT_PROJECTS: dict[str, Any] = {
    "content": [{"id": "project-1", "name": "proj-a", "organizationId": "org-1"}],
    "totalElements": 1,
    "totalPages": 1,
}
_TENANT_ABOUT: dict[str, Any] = {
    "latestApiVersion": "9.0",
    "supportedApis": [{"apiVersion": "9.0"}],
}

#: op_id -> the JSON body its respx route returns.
_PAYLOAD_BY_OP: dict[str, dict[str, Any]] = {
    "vcfa.provider.org.list": _PROVIDER_ORGS,
    "vcfa.provider.region.list": _PROVIDER_REGIONS,
    "vcfa.provider.health": _PROVIDER_SITE,
    "vcfa.tenant.project.list": _TENANT_PROJECTS,
    "vcfa.tenant.about": _TENANT_ABOUT,
}


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _required_settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Pin env vars :class:`Settings` requires for this module."""
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    from meho_backplane.settings import get_settings

    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _reset_module_state() -> Iterator[None]:
    """Reset dispatcher + handler caches around every test."""
    reset_dispatcher_caches()
    reset_handler_cache()
    yield
    reset_dispatcher_caches()
    reset_handler_cache()


@pytest.fixture(autouse=True)
def _stub_embedding(monkeypatch: pytest.MonkeyPatch) -> None:
    """Deterministic embedding stub so typed-op registration doesn't pull ONNX."""
    monkeypatch.setattr(
        "meho_backplane.operations.typed_register.encode_endpoint_text",
        AsyncMock(return_value=[0.1] * 384),
    )


@pytest.fixture
def captured_events(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    """Stub :func:`publish_event` so the broadcast bus doesn't fire."""
    events: list[Any] = []

    async def _capture(event: Any) -> None:
        events.append(event)

    monkeypatch.setattr(audit_module, "publish_event", _capture)
    return events


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _stub_credentials_loader(_target: object, _operator: Operator) -> dict[str, str]:
    """Stub loader — bypasses the live Vault read for the dispatch tests."""
    return {"username": "svc-meho", "password": "vcfa-typed-password"}


async def _seed_target(*, host: str, fqdn: str | None) -> Target:
    """Insert the typed-reads Target row and return it (expunged)."""
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        target = Target(
            tenant_id=_OPERATOR_TENANT,
            name=_TARGET_NAME,
            aliases=[],
            product=VcfAutomationConnector.product,
            host=host,
            port=443,
            fqdn=fqdn,
            secret_ref="vcfa/typed",
            auth_model="shared_service_account",
            vpn_required=False,
            extras={},
            fingerprint=_FINGERPRINT,
            notes="seeded by test_connectors_vcf_automation_typed_reads._seed_target",
        )
        session.add(target)
        await session.commit()
        await session.refresh(target)
        session.expunge(target)
        return target


def _resolve_connector() -> VcfAutomationConnector:
    """Resolve + cache the connector instance with a stubbed credentials loader."""
    registry = all_connectors_v2()
    connector_cls = registry.get((_REGISTRY_PRODUCT, VCFA_VERSION, VCFA_IMPL_ID))
    if connector_cls is None:
        import importlib

        import meho_backplane.connectors.vcf_automation as _vcfa_pkg

        importlib.reload(_vcfa_pkg)
        registry = all_connectors_v2()
        connector_cls = registry.get((_REGISTRY_PRODUCT, VCFA_VERSION, VCFA_IMPL_ID))
    assert connector_cls is VcfAutomationConnector
    instance = get_or_create_connector_instance(connector_cls)
    instance._credentials_loader = _stub_credentials_loader  # type: ignore[attr-defined]
    return instance


def _register_routes(mock: respx.MockRouter, *, capture: dict[str, str] | None = None) -> None:
    """Register the dual-plane login + five typed-op GET routes on *mock*.

    When *capture* is passed, every GET records its ``Accept`` header under
    the request path so the plane-selection tests can assert the media type.
    """
    mock.post("/cloudapi/1.0.0/sessions/provider").respond(
        200, headers={"X-VMWARE-VCLOUD-ACCESS-TOKEN": _PROVIDER_JWT}
    )
    mock.post("/iaas/api/login").respond(200, json={"token": _TENANT_TOKEN})

    for op in VCFA_TYPED_OPS:
        payload = _PAYLOAD_BY_OP[op.op_id]

        def _responder(
            request: httpx.Request, _payload: dict[str, Any] = payload
        ) -> httpx.Response:
            if capture is not None:
                capture[request.url.path] = request.headers.get("Accept", "")
            return httpx.Response(200, json=_payload)

        mock.get(op.path).mock(side_effect=_responder)


@dataclass(frozen=True)
class _Bundle:
    connector_instance: VcfAutomationConnector
    db_target: Any


@pytest.fixture
async def typed_bundle(captured_events: list[Any]) -> AsyncIterator[_Bundle]:
    """Register the five typed ops (zero ingested state) + respx-mock the appliance."""
    await VcfAutomationConnector.register_typed_operations()
    seeded = await _seed_target(host="10.20.30.5", fqdn=_FQDN)
    instance = _resolve_connector()
    async with respx.mock(
        base_url=_BASE_URL, assert_all_called=False, assert_all_mocked=False
    ) as m:
        _register_routes(m)
        try:
            yield _Bundle(connector_instance=instance, db_target=seeded)
        finally:
            await instance.aclose()
            reset_dispatcher_caches()


# ---------------------------------------------------------------------------
# Registration shape
# ---------------------------------------------------------------------------


async def test_typed_ops_register_as_source_kind_typed_with_zero_ingested_rows() -> None:
    """After registration every audited op is a ``typed`` row; no ingested rows exist."""
    await VcfAutomationConnector.register_typed_operations()

    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        rows = (
            (
                await session.execute(
                    select(EndpointDescriptor).where(
                        EndpointDescriptor.product == VCFA_PRODUCT,
                        EndpointDescriptor.version == VCFA_VERSION,
                        EndpointDescriptor.impl_id == VCFA_IMPL_ID,
                    )
                )
            )
            .scalars()
            .all()
        )

    by_op = {row.op_id: row for row in rows}
    expected = {op.op_id for op in VCFA_TYPED_OPS}
    assert expected.issubset(by_op), f"missing typed rows: {expected - set(by_op)}"
    for op in VCFA_TYPED_OPS:
        row = by_op[op.op_id]
        assert row.source_kind == "typed", f"{op.op_id} source_kind={row.source_kind!r}"
        assert row.handler_ref and op.handler_attr in row.handler_ref, row.handler_ref
        assert row.safety_level == "safe"
        assert row.requires_approval is False
    # Zero catalog state: nothing was ingested for this connector.
    assert not [r for r in rows if r.source_kind == "ingested"], (
        "typed conversion must not depend on any ingested endpoint_descriptor row"
    )


# ---------------------------------------------------------------------------
# Plane declaration (static)
# ---------------------------------------------------------------------------


def test_every_typed_op_declares_the_plane_its_path_rides() -> None:
    """Each op's declared ``plane`` matches ``plane_for_path(op.path)``.

    The import-time ``_validate_typed_op_planes()`` already enforces this;
    pinning it as an explicit test documents the contract that a declared
    plane / path drift is a hard failure (it would otherwise surface as a
    misrouted HTTP 401 — both planes carry a Bearer header but reject the
    other plane's token).
    """
    assert len(VCFA_TYPED_OPS) == 5
    for op in VCFA_TYPED_OPS:
        assert plane_for_path(op.path) == op.plane, (
            f"op {op.op_id!r} declares plane={op.plane!r} but "
            f"plane_for_path({op.path!r})={plane_for_path(op.path)!r}"
        )


# ---------------------------------------------------------------------------
# Live typed dispatch — fresh boot, zero catalog state
# ---------------------------------------------------------------------------

_TYPED_OP_IDS: tuple[str, ...] = tuple(op.op_id for op in VCFA_TYPED_OPS)


@pytest.mark.parametrize("op_id", _TYPED_OP_IDS, ids=lambda op: op)
async def test_typed_ops_dispatch_ok(op_id: str, typed_bundle: _Bundle) -> None:
    """All five typed ops dispatch through ``call_operation`` and return ``status='ok'``."""
    result = await call_operation(
        _OPERATOR,
        {
            "connector_id": VCFA_CONNECTOR_ID,
            "op_id": op_id,
            "target": {"name": _TARGET_NAME},
            "params": {},
        },
    )
    assert result["status"] == "ok", (
        f"typed op {op_id!r} did not return status='ok': "
        f"error={result.get('error')!r} full={result!r}"
    )


# ---------------------------------------------------------------------------
# Plane selection — provider vs tenant session, cross-plane isolation
# ---------------------------------------------------------------------------


async def test_provider_typed_op_rides_provider_plane(captured_events: list[Any]) -> None:
    """Provider typed op establishes the provider session + Accept; tenant cache stays empty."""
    await VcfAutomationConnector.register_typed_operations()
    seeded = await _seed_target(host="10.20.30.5", fqdn=_FQDN)
    instance = _resolve_connector()
    cache_key = target_cache_key(seeded)
    accept_by_path: dict[str, str] = {}

    try:
        async with respx.mock(
            base_url=_BASE_URL, assert_all_called=False, assert_all_mocked=False
        ) as m:
            _register_routes(m, capture=accept_by_path)
            result = await call_operation(
                _OPERATOR,
                {
                    "connector_id": VCFA_CONNECTOR_ID,
                    "op_id": "vcfa.provider.health",
                    "target": {"name": _TARGET_NAME},
                    "params": {},
                },
            )
            # Snapshot the caches before aclose() (in finally) clears them.
            provider_cached = instance._provider_tokens.get(cache_key)
            tenant_cached = instance._tenant_tokens.get(cache_key)
    finally:
        await instance.aclose()
        reset_dispatcher_caches()

    assert result["status"] == "ok", result
    # Provider session established; tenant plane never touched.
    assert provider_cached == _PROVIDER_JWT
    assert tenant_cached is None, (
        f"provider-only dispatch must not establish the tenant session; got {tenant_cached!r}"
    )
    # The request rode the provider plane's Accept media type.
    assert accept_by_path.get("/cloudapi/1.0.0/site") == PROVIDER_CLOUDAPI_ACCEPT, accept_by_path


async def test_tenant_typed_op_rides_tenant_plane(captured_events: list[Any]) -> None:
    """Tenant typed op establishes the tenant session + Accept; provider cache stays empty."""
    await VcfAutomationConnector.register_typed_operations()
    seeded = await _seed_target(host="10.20.30.5", fqdn=_FQDN)
    instance = _resolve_connector()
    cache_key = target_cache_key(seeded)
    accept_by_path: dict[str, str] = {}

    try:
        async with respx.mock(
            base_url=_BASE_URL, assert_all_called=False, assert_all_mocked=False
        ) as m:
            _register_routes(m, capture=accept_by_path)
            result = await call_operation(
                _OPERATOR,
                {
                    "connector_id": VCFA_CONNECTOR_ID,
                    "op_id": "vcfa.tenant.about",
                    "target": {"name": _TARGET_NAME},
                    "params": {},
                },
            )
            # Snapshot the caches before aclose() (in finally) clears them.
            tenant_cached = instance._tenant_tokens.get(cache_key)
            provider_cached = instance._provider_tokens.get(cache_key)
    finally:
        await instance.aclose()
        reset_dispatcher_caches()

    assert result["status"] == "ok", result
    # Tenant session established; provider plane never touched.
    assert tenant_cached == _TENANT_TOKEN
    assert provider_cached is None, (
        f"tenant-only dispatch must not establish the provider session; got {provider_cached!r}"
    )
    # The request rode the tenant plane's plain-JSON Accept media type.
    assert accept_by_path.get("/iaas/api/about") == TENANT_ACCEPT, accept_by_path


async def test_provider_op_query_params_forward_pagination(captured_events: list[Any]) -> None:
    """``vcfa.provider.org.list`` forwards ``page`` / ``pageSize`` as query params."""
    await VcfAutomationConnector.register_typed_operations()
    await _seed_target(host="10.20.30.5", fqdn=_FQDN)
    instance = _resolve_connector()
    captured: dict[str, str] = {}

    def _orgs_responder(request: httpx.Request) -> httpx.Response:
        captured["query"] = str(request.url.query.decode())
        return httpx.Response(200, json=_PROVIDER_ORGS)

    try:
        async with respx.mock(
            base_url=_BASE_URL, assert_all_called=False, assert_all_mocked=False
        ) as m:
            m.post("/cloudapi/1.0.0/sessions/provider").respond(
                200, headers={"X-VMWARE-VCLOUD-ACCESS-TOKEN": _PROVIDER_JWT}
            )
            m.get("/cloudapi/1.0.0/orgs").mock(side_effect=_orgs_responder)
            result = await call_operation(
                _OPERATOR,
                {
                    "connector_id": VCFA_CONNECTOR_ID,
                    "op_id": "vcfa.provider.org.list",
                    "target": {"name": _TARGET_NAME},
                    "params": {"page": 2, "pageSize": 50},
                },
            )
    finally:
        await instance.aclose()
        reset_dispatcher_caches()

    assert result["status"] == "ok", result
    assert "page=2" in captured["query"] and "pageSize=50" in captured["query"], captured
