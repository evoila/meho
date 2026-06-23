# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""vROps E2E recorded-fixture integration test (G3.6-T3 #837).

Covers all four acceptance criteria from Issue #837:

(a) All 8 curated vROps read ops dispatch through the full
    ``call_operation`` stack against a respx-mocked vROps appliance
    and return ``status='ok'``.
(b) HTTP Basic auth path: vROps' ``/suite-api/api/*`` surface is
    stateless — no session token, no 401-retry. The connector's stub
    credentials loader is verified to be called (once, then cached)
    on the first dispatch — mirrors the SDDC Manager Basic-auth
    posture, not NSX's session-cookie-with-401-retry posture.
(c) Audit rows: each dispatch inserts an ``AuditLog`` row carrying
    ``method='DISPATCH'``, a non-null ``target_id``, and a
    ``payload["params_hash"]`` key.
(d) JSONFlux handle path: ``GET:/suite-api/api/resources`` dispatched
    with the real
    :class:`~meho_backplane.operations.jsonflux_reducer.JsonFluxReducer`
    in force mode (``row_threshold=0``) returns a populated
    ``OperationResult.handle`` with at least one ``sample_rows`` entry.

Database: SQLite via the autouse ``_default_database_url`` conftest
fixture (no ``pg_engine`` required). Runs in the ``meho-runners`` CI
lane alongside the other ``backend/tests/test_connectors_*.py``
files; no Docker dependency.

Why recorded-fixture and not a live appliance: vROps has no public
CI simulator (#536 — same constraint NSX, SDDC Manager, and Harbor
hit). respx mocks the exact wire contract the connector calls, so
the Basic-auth header + per-op HTTP request all fire through the
connector's real pooled :class:`httpx.AsyncClient`.

Fixture-refresh tooling: ``backend/tests/fixtures/vcf/refresh.py``
(shipped by #841) is the operator-run tool that captures real vROps
responses against a live appliance and writes redacted JSON
fixtures. This E2E uses the same canary payloads
:mod:`tests.acceptance._vrops_canary_fixtures` carries (which mirror
the shape ``refresh.py`` would write); the seam between the two is
the per-op JSON payload, so a future task can switch this E2E to
load from the ``backend/tests/fixtures/vcf/vcf-operations/*.json``
directory without changing the dispatcher-leg assertions.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
from typing import Any

import pytest
import respx
from sqlalchemy import select

import meho_backplane.operations._audit as audit_module
from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors._shared.cache_key import target_cache_key
from meho_backplane.connectors.registry import all_connectors_v2
from meho_backplane.connectors.vcf_operations import (
    VROPS_CONNECTOR_ID,
    VROPS_CORE_OPS,
    VROPS_IMPL_ID,
    VROPS_VERSION,
    VcfOperationsConnector,
)
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import AuditLog, Target
from meho_backplane.operations import reset_dispatcher_caches
from meho_backplane.operations._handler_resolve import get_or_create_connector_instance
from meho_backplane.operations.dispatcher import set_default_reducer
from meho_backplane.operations.jsonflux_reducer import JsonFluxReducer
from meho_backplane.operations.meta_tools import call_operation
from meho_backplane.operations.reducer import PassThroughReducer
from tests.acceptance._vrops_canary_fixtures import (
    VROPS_CANARY_BASE_URL,
    VROPS_CANARY_FINGERPRINT,
    VROPS_CANARY_OPERATOR_TENANT,
    VROPS_CANARY_RESOURCES,
    VROPS_FORCE_HANDLE_LIST_OP_ID,
    _insert_vrops_descriptors,
    _register_vrops_routes,
    _vrops_credentials_loader,
)

# ---------------------------------------------------------------------------
# Module-level constants
# ---------------------------------------------------------------------------

# Target.product matches the connector class's v2-registry key
# ("vrops"), not the parse_connector_id-derived "vrops" that
# descriptor / group rows carry. See
# tests.acceptance._vrops_canary_fixtures module docstring,
# "EndpointDescriptor.product vs Target.product".
_TARGET_PRODUCT = "vrops"

_OPERATOR = Operator(
    sub="vcf-operations-e2e-test",
    name="vROps E2E Test Operator",
    email=None,
    raw_jwt="<vrops-e2e-raw-jwt>",
    tenant_id=VROPS_CANARY_OPERATOR_TENANT,
    tenant_role=TenantRole.TENANT_ADMIN,
)

_E2E_TARGET_NAME = "vcf-operations-e2e-target"

# Path-template params for the one op whose URL carries ``{id}``.
# The other 7 curated ops have no path params.
_RESOURCE_GET_OP_PARAMS: dict[str, dict[str, object]] = {
    "GET:/suite-api/api/resources/{id}": {
        "id": "00000000-0000-4000-8000-000000000000",
    },
}

# ---------------------------------------------------------------------------
# Module-level autouse fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _required_settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Pin env vars that :class:`Settings` requires for this module."""
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    from meho_backplane.settings import get_settings

    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _reset_module_state() -> Iterator[None]:
    """Reset dispatcher caches around every test."""
    reset_dispatcher_caches()
    yield
    reset_dispatcher_caches()


@pytest.fixture
def captured_events(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    """Stub out :func:`publish_event` so the broadcast bus doesn't fire.

    Required for the same reason as every other DB-backed dispatcher
    test: the real broadcast bus tries to write to a Redis stream
    that doesn't exist in the unit-test environment.
    """
    events: list[Any] = []

    async def _capture(event: Any) -> None:
        events.append(event)

    monkeypatch.setattr(audit_module, "publish_event", _capture)
    return events


# ---------------------------------------------------------------------------
# Setup helpers
# ---------------------------------------------------------------------------


async def _seed_target() -> Any:
    """Insert the E2E target row and return it (expunged from the session)."""
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        target = Target(
            tenant_id=VROPS_CANARY_OPERATOR_TENANT,
            name=_E2E_TARGET_NAME,
            aliases=[],
            product=_TARGET_PRODUCT,
            host=VROPS_CANARY_BASE_URL.removeprefix("https://"),
            port=443,
            fqdn=None,
            secret_ref="vcf-operations/vcf-operations-e2e",
            auth_model="shared_service_account",
            vpn_required=False,
            extras={},
            fingerprint=VROPS_CANARY_FINGERPRINT,
            notes="seeded by test_connectors_vcf_operations_e2e._seed_target",
        )
        session.add(target)
        await session.commit()
        await session.refresh(target)
        session.expunge(target)
        return target


def _resolve_connector() -> VcfOperationsConnector:
    """Resolve + cache the VcfOperationsConnector instance with a stubbed loader."""
    registry = all_connectors_v2()
    connector_cls = registry.get((_TARGET_PRODUCT, VROPS_VERSION, VROPS_IMPL_ID))
    assert connector_cls is VcfOperationsConnector, (
        f"VcfOperationsConnector not registered for "
        f"({_TARGET_PRODUCT}, {VROPS_VERSION}, {VROPS_IMPL_ID}); got {connector_cls!r}"
    )
    instance = get_or_create_connector_instance(connector_cls)
    # Patch the shared CredentialsCache's loader so no Vault read fires.
    instance._creds._loader = _vrops_credentials_loader  # type: ignore[attr-defined]
    return instance


# ---------------------------------------------------------------------------
# Primary E2E fixture
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _VropsE2EBundle:
    target_name: str
    connector_instance: VcfOperationsConnector
    db_target: Any


@pytest.fixture
async def vcf_operations_e2e_canary(captured_events: list[Any]) -> AsyncIterator[_VropsE2EBundle]:
    """Dispatcher-ready vROps setup over a respx-mocked appliance (happy-path).

    Lifecycle:
    1. Insert the 8 curated
       :data:`~meho_backplane.connectors.vcf_operations.VROPS_CORE_OPS`
       descriptors + their 7 groups into the per-test SQLite DB.
    2. Seed a :class:`Target` row carrying
       :data:`VROPS_CANARY_FINGERPRINT` so the resolver binds
       :class:`VcfOperationsConnector`.
    3. Resolve + cache the connector instance; patch the shared
       :class:`CredentialsCache`'s loader so no Vault read fires.
    4. Activate a respx router for :data:`VROPS_CANARY_BASE_URL` and
       register the 8 read-op routes against pre-seeded canary
       payloads.
    """
    await _insert_vrops_descriptors()
    seeded_target = await _seed_target()
    instance = _resolve_connector()

    async with respx.mock(
        base_url=VROPS_CANARY_BASE_URL,
        assert_all_called=False,
        assert_all_mocked=False,
    ) as mock:
        _register_vrops_routes(mock)
        try:
            yield _VropsE2EBundle(
                target_name=_E2E_TARGET_NAME,
                connector_instance=instance,
                db_target=seeded_target,
            )
        finally:
            await instance.aclose()
            reset_dispatcher_caches()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

_OP_IDS: tuple[str, ...] = tuple(op.op_id for op in VROPS_CORE_OPS)
assert len(_OP_IDS) == 8, f"Expected 8 curated vROps ops, got {len(_OP_IDS)}: {_OP_IDS}"


@pytest.mark.parametrize("op_id", _OP_IDS, ids=lambda op: op)
async def test_vcf_operations_e2e_all_ops_dispatch_ok(
    op_id: str,
    vcf_operations_e2e_canary: _VropsE2EBundle,
) -> None:
    """All 8 vROps core ops dispatch through the full dispatcher and return status='ok'.

    Acceptance criterion (a). HTTP Basic auth is computed by the
    connector's stub credentials loader on first dispatch (then
    cached); no session-establish step is needed because Basic auth
    is stateless on the vROps side. The parametrise reports one CI
    case per op_id for granular failure attribution.
    """
    params = _RESOURCE_GET_OP_PARAMS.get(op_id, {})
    result = await call_operation(
        _OPERATOR,
        {
            "connector_id": VROPS_CONNECTOR_ID,
            "op_id": op_id,
            "target": {"name": vcf_operations_e2e_canary.target_name},
            "params": params,
        },
    )
    assert result["status"] == "ok", (
        f"vROps op {op_id!r} did not return status='ok': "
        f"error={result.get('error')!r} full={result!r}"
    )


async def test_vcf_operations_e2e_credentials_cached_after_first_dispatch(
    vcf_operations_e2e_canary: _VropsE2EBundle,
) -> None:
    """First dispatch loads credentials; subsequent dispatches reuse the cache.

    Acceptance criterion (b). vROps' Basic auth is stateless server-
    side — there's no session token to refresh and no 401-retry. The
    only thing the connector caches is the Vault-sourced
    ``{"username": ..., "password": ...}`` mapping behind
    :class:`CredentialsCache`. Pin the contract: cache empty on
    first reach, populated after one dispatch, and not re-filled on
    the second dispatch to the same target.

    Mirrors :func:`test_sddc_e2e_credentials_cached_after_first_dispatch`
    in the SDDC Manager E2E. NSX's analogue tests a 401-retry loop
    that doesn't apply to vROps.
    """
    instance = vcf_operations_e2e_canary.connector_instance
    target_name = vcf_operations_e2e_canary.target_name
    # The shared CredentialsCache holds entries keyed by the tenant-unique
    # (tenant_id, id) tuple (#1642), not the bare name.
    cache_key = target_cache_key(vcf_operations_e2e_canary.db_target)

    assert cache_key not in instance._creds._cache, (
        "Expected empty credential cache before first dispatch; "
        f"got _creds._cache={list(instance._creds._cache.keys())!r}"
    )

    result = await call_operation(
        _OPERATOR,
        {
            "connector_id": VROPS_CONNECTOR_ID,
            "op_id": "GET:/suite-api/api/versions/current",
            "target": {"name": target_name},
            "params": {},
        },
    )
    assert result["status"] == "ok"
    assert cache_key in instance._creds._cache, (
        "Expected credentials cached after first dispatch; "
        f"got _creds._cache={list(instance._creds._cache.keys())!r}"
    )

    # Second dispatch must NOT reload credentials — cache entry survives.
    creds_before = dict(instance._creds._cache)
    result2 = await call_operation(
        _OPERATOR,
        {
            "connector_id": VROPS_CONNECTOR_ID,
            "op_id": "GET:/suite-api/api/resources",
            "target": {"name": target_name},
            "params": {},
        },
    )
    assert result2["status"] == "ok"
    assert instance._creds._cache == creds_before, (
        "Credential cache should not change on second dispatch (cache hit); "
        f"before={creds_before!r} after={instance._creds._cache!r}"
    )


async def test_vcf_operations_e2e_dispatch_writes_audit_row(
    vcf_operations_e2e_canary: _VropsE2EBundle,
) -> None:
    """Each dispatch inserts an AuditLog row with method='DISPATCH', target_id, params_hash.

    Acceptance criterion (c). Dispatches one op and asserts:

    * Exactly one new ``AuditLog`` row with ``method='DISPATCH'`` and
      ``path == op_id``.
    * ``row.target_id`` is not None (the Target row was resolved and
      its UUID was recorded).
    * ``row.payload["op_id"]`` equals the dispatched ``op_id``.
    * ``row.payload["params_hash"]`` is present (non-None string).
    """
    op_id = "GET:/suite-api/api/resources"
    sessionmaker = get_sessionmaker()

    async def _count_dispatch_rows() -> int:
        async with sessionmaker() as session:
            result = await session.execute(
                select(AuditLog).where(
                    AuditLog.method == "DISPATCH",
                    AuditLog.path == op_id,
                )
            )
            return len(list(result.scalars().all()))

    baseline = await _count_dispatch_rows()

    result = await call_operation(
        _OPERATOR,
        {
            "connector_id": VROPS_CONNECTOR_ID,
            "op_id": op_id,
            "target": {"name": vcf_operations_e2e_canary.target_name},
            "params": {},
        },
    )
    assert result["status"] == "ok"

    final = await _count_dispatch_rows()
    assert final - baseline == 1, (
        f"Expected exactly one new DISPATCH row for {op_id!r}; baseline={baseline} final={final}"
    )

    async with sessionmaker() as session:
        row_result = await session.execute(
            select(AuditLog)
            .where(
                AuditLog.method == "DISPATCH",
                AuditLog.path == op_id,
            )
            .order_by(AuditLog.id.desc())
            .limit(1)
        )
        row = row_result.scalars().first()

    assert row is not None
    assert row.target_id is not None, (
        "AuditLog.target_id must not be None for a targeted dispatch; "
        f"got target_id=None on row {row!r}"
    )
    assert row.payload.get("op_id") == op_id, (
        f"AuditLog.payload['op_id'] must equal the dispatched op_id; got payload={row.payload!r}"
    )
    assert row.payload.get("params_hash"), (
        f"AuditLog.payload must carry a non-empty 'params_hash'; got payload={row.payload!r}"
    )


async def test_vcf_operations_e2e_jsonflux_handle_populated_for_resource_list(
    vcf_operations_e2e_canary: _VropsE2EBundle,
) -> None:
    """Resource list dispatched with the real JsonFluxReducer returns a populated handle.

    Acceptance criterion (d). Resource list is the largest list
    surface in a real vROps deployment (hundreds to thousands of
    monitored objects), mirroring the NSX segment-list / SDDC
    host-list choices.

    Assertions:

    * ``status == 'ok'``.
    * ``handle`` is non-None and carries a valid UUID4 ``handle_id``.
    * ``handle.total_rows`` matches the seeded resource count.
    * ``handle.sample_rows`` is populated (>=1 row).
    * ``result['row_count']`` equals ``handle.total_rows``.
    """
    resource_list = VROPS_CANARY_RESOURCES["resourceList"]
    assert isinstance(resource_list, list)
    expected_rows = len(resource_list)

    set_default_reducer(JsonFluxReducer(row_threshold=0))
    try:
        result_envelope = await call_operation(
            _OPERATOR,
            {
                "connector_id": VROPS_CONNECTOR_ID,
                "op_id": VROPS_FORCE_HANDLE_LIST_OP_ID,
                "target": {"name": vcf_operations_e2e_canary.target_name},
                "params": {},
            },
        )
    finally:
        set_default_reducer(PassThroughReducer())

    assert result_envelope["status"] == "ok", (
        f"Expected JSONFlux dispatch to succeed; got {result_envelope!r}"
    )

    handle = result_envelope.get("handle")
    assert handle is not None, (
        "Expected OperationResult.handle to be populated by JsonFluxReducer; "
        f"got handle=None on envelope={result_envelope!r}"
    )

    uuid.UUID(handle["handle_id"])

    assert handle["total_rows"] == expected_rows, (
        f"Expected {expected_rows} resource rows from VROPS_CANARY_RESOURCES; "
        f"got handle.total_rows={handle['total_rows']}"
    )

    sample_rows = handle.get("sample_rows")
    assert sample_rows, (
        "Expected >=1 sample row from the seeded vROps resource list; "
        f"got sample_rows={sample_rows!r}"
    )

    payload = result_envelope.get("result")
    assert payload is not None and payload.get("row_count") == expected_rows, (
        f"Expected reducer summary on result.row_count={expected_rows}; got result={payload!r}"
    )
