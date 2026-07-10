# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""VCF Fleet E2E recorded-fixture integration test (G3.6-T9 #839).

Covers all five acceptance criteria from Issue #839:

(a) `meho vcf-fleet --help` lists the verb set; each verb POSTs to
    `/api/v1/operations/call` with the right `op_id`. The CLI-side
    assertion lives in :mod:`cli.internal.cmd.vcf-fleet.vcf_fleet_test`
    (Go); this module covers the **backend half** — every enabled
    Fleet op dispatches through the full ``call_operation`` stack
    against a respx-mocked appliance.
(b) `backend/tests/test_connectors_vcf_fleet_e2e.py` (this file)
    replays a captured fixture for every enabled op and passes in the
    ``meho-runners`` CI lane with no Docker dependency.
(c) At least one list op's E2E asserts the JSONFlux handle path
    (handle → ``result_query`` drills in) — see
    :func:`test_fleet_e2e_jsonflux_handle_populated_for_environment_list`.
(d) All enabled ops write an audit row carrying ``op_id`` + ``target_id``
    + ``params_hash`` — see :func:`test_fleet_e2e_dispatch_writes_audit_row`.
(e) ``docs/cross-repo/vcf-fleet-onboarding.md`` exists with the
    wrapper-flip recipe (verified on the doc side, not asserted here).

Database: SQLite via the autouse ``_default_database_url`` conftest
fixture (no ``pg_engine`` required). Runs in the ``meho-runners`` CI
lane alongside the other ``backend/tests/test_connectors_*.py`` files;
no Docker dependency.

Auth shape note
---------------

Fleet uses HTTP Basic on every request (no session-establish call) —
mirrors the SDDC Manager E2E test's "credentials cached after first
dispatch" assertion. There is no 401-retry test here because Fleet's
local user store has no session lifecycle; a 401 means wrong
credentials, terminal.
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
from meho_backplane.connectors.vcf_fleet import (
    FLEET_CONNECTOR_ID,
    FLEET_CORE_OPS,
    VcfFleetConnector,
)
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import AuditLog, Target
from meho_backplane.operations import reset_dispatcher_caches
from meho_backplane.operations._handler_resolve import get_or_create_connector_instance
from meho_backplane.operations.dispatcher import set_default_reducer
from meho_backplane.operations.jsonflux_reducer import JsonFluxReducer
from meho_backplane.operations.meta_tools import call_operation
from meho_backplane.operations.reducer import PassThroughReducer
from tests.acceptance._vcf_fleet_canary_fixtures import (
    FLEET_CANARY_BASE_URL,
    FLEET_CANARY_FINGERPRINT,
    FLEET_CANARY_OPERATOR_TENANT,
    FLEET_CANARY_REQUESTS,
    FLEET_FORCE_HANDLE_LIST_OP_ID,
    FLEET_TARGET_NAME,
    _fleet_credentials_loader,
    _insert_fleet_descriptors,
    _register_fleet_routes,
)

# ---------------------------------------------------------------------------
# Module-level constants
# ---------------------------------------------------------------------------

_OPERATOR = Operator(
    sub="fleet-e2e-test",
    name="Fleet E2E Test Operator",
    email=None,
    raw_jwt="<fleet-e2e-raw-jwt>",
    tenant_id=FLEET_CANARY_OPERATOR_TENANT,
    tenant_role=TenantRole.TENANT_ADMIN,
)

# Path-template params for the ops whose op_ids carry a ``{placeholder}``.
# The dispatcher's ``_substitute_path`` substitutes the literal at
# dispatch time; the respx routes are registered against the
# substituted paths (see ``_register_fleet_routes``).
_OP_PARAMS: dict[str, dict[str, object]] = {
    "GET:/lcm/lcops/api/v2/datacenters/{dataCenterVmid}/vcenters": {
        "dataCenterVmid": "dc-canary-001",
    },
    "GET:/lcm/lcops/api/v2/environments/{environmentId}": {
        "environmentId": "env-canary-000",
    },
    "GET:/lcm/lcops/api/v2/environments/{environmentId}/products": {
        "environmentId": "env-canary-000",
    },
    "GET:/lcm/request/api/v2/requests/{requestId}": {
        "requestId": "req-canary-002",
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
    test: the real broadcast bus tries to write to a Redis stream that
    doesn't exist in the unit-test environment.
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
            tenant_id=FLEET_CANARY_OPERATOR_TENANT,
            name=FLEET_TARGET_NAME,
            aliases=[],
            product="fleet",
            host=FLEET_CANARY_BASE_URL.removeprefix("https://"),
            port=443,
            fqdn=None,
            secret_ref="vcf-fleet/fleet-e2e",
            auth_model="shared_service_account",
            vpn_required=False,
            extras={},
            fingerprint=FLEET_CANARY_FINGERPRINT,
            notes="seeded by test_connectors_vcf_fleet_e2e._seed_target",
        )
        session.add(target)
        await session.commit()
        await session.refresh(target)
        session.expunge(target)
        return target


def _resolve_connector() -> VcfFleetConnector:
    """Resolve + cache the VcfFleetConnector instance with a stubbed credentials loader."""
    registry = all_connectors_v2()
    connector_cls = registry.get(("fleet", "9.0", "fleet-rest"))
    assert connector_cls is VcfFleetConnector, (
        f"VcfFleetConnector not registered for (vcf-fleet, 9.0, fleet-rest); got {connector_cls!r}"
    )
    instance = get_or_create_connector_instance(connector_cls)
    # Inject the stub credentials loader so no Vault read fires. The
    # CredentialsCache's loader is constructed at __init__; reaching in
    # here mirrors what the SDDC Manager E2E test does on its
    # ``_credentials_loader`` attr.
    instance._creds = type(instance._creds)(  # type: ignore[attr-defined]
        _fleet_credentials_loader,
        product_label="fleet",
    )
    return instance


# ---------------------------------------------------------------------------
# Primary E2E fixture (happy-path + audit rows)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _FleetE2EBundle:
    target_name: str
    connector_instance: VcfFleetConnector
    db_target: Any


@pytest.fixture
async def fleet_e2e_canary(captured_events: list[Any]) -> AsyncIterator[_FleetE2EBundle]:
    """Dispatcher-ready Fleet setup over a respx-mocked appliance (happy-path).

    Lifecycle:

    1. Insert :data:`~meho_backplane.connectors.vcf_fleet.FLEET_CORE_OPS`
       descriptors + groups into the per-test SQLite DB.
    2. Seed a :class:`Target` row carrying :data:`FLEET_CANARY_FINGERPRINT`
       so the resolver binds :class:`VcfFleetConnector`.
    3. Resolve + cache the connector instance; replace its
       :class:`CredentialsCache` so no Vault read fires.
    4. Activate a respx router for :data:`FLEET_CANARY_BASE_URL` and
       register the Fleet read-op routes.
    """
    await _insert_fleet_descriptors()
    seeded_target = await _seed_target()
    instance = _resolve_connector()

    async with respx.mock(
        base_url=FLEET_CANARY_BASE_URL,
        assert_all_called=False,
        assert_all_mocked=False,
    ) as mock:
        _register_fleet_routes(mock)
        try:
            yield _FleetE2EBundle(
                target_name=FLEET_TARGET_NAME,
                connector_instance=instance,
                db_target=seeded_target,
            )
        finally:
            await instance.aclose()
            reset_dispatcher_caches()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

_OP_IDS: tuple[str, ...] = tuple(op.op_id for op in FLEET_CORE_OPS)
assert len(_OP_IDS) == 6, f"Expected 6 curated Fleet ops, got {len(_OP_IDS)}: {_OP_IDS}"


@pytest.mark.parametrize("op_id", _OP_IDS, ids=lambda op: op)
async def test_fleet_e2e_all_ops_dispatch_ok(
    op_id: str,
    fleet_e2e_canary: _FleetE2EBundle,
) -> None:
    """All ingested Fleet core ops dispatch through the full dispatcher, status='ok'.

    HTTP Basic auth is computed by the connector's stub credentials
    loader on first dispatch (then cached); no session-establish step
    is needed because Basic auth is stateless on the Fleet side. The
    parametrise reports one CI case per op_id for granular failure
    attribution.
    """
    params = _OP_PARAMS.get(op_id, {})
    result = await call_operation(
        _OPERATOR,
        {
            "connector_id": FLEET_CONNECTOR_ID,
            "op_id": op_id,
            "target": {"name": fleet_e2e_canary.target_name},
            "params": params,
        },
    )
    assert result["status"] == "ok", (
        f"Fleet op {op_id!r} did not return status='ok': "
        f"error={result.get('error')!r} full={result!r}"
    )


async def test_fleet_e2e_credentials_cached_after_first_dispatch(
    fleet_e2e_canary: _FleetE2EBundle,
) -> None:
    """First dispatch loads credentials; subsequent dispatches reuse the cache.

    Verifies the HTTP Basic-auth half of acceptance criterion (a):
    the Fleet connector's per-target credential cache (a
    :class:`~meho_backplane.connectors._shared.vcf_auth.CredentialsCache`)
    starts empty, gets populated on the first dispatch, and is not
    re-filled on the second dispatch to the same target. HTTP Basic is
    stateless server-side — no session revoke or re-login needed.
    """
    instance = fleet_e2e_canary.connector_instance
    target_name = fleet_e2e_canary.target_name
    # The CredentialsCache exposes the underlying dict via ``_cache``; the
    # SDDC Manager precedent uses ``_creds_cache`` because its connector
    # caches the dict directly. The cache key is the tenant-unique
    # (tenant_id, id) tuple (#1642), set by ``CredentialsCache.get(target)``.
    cache_key = target_cache_key(fleet_e2e_canary.db_target)
    initial_keys = list(instance._creds._cache.keys())  # type: ignore[attr-defined]
    assert cache_key not in initial_keys, (
        f"Expected empty credential cache before first dispatch; got keys={initial_keys!r}"
    )

    result = await call_operation(
        _OPERATOR,
        {
            "connector_id": FLEET_CONNECTOR_ID,
            "op_id": "GET:/lcm/lcops/api/v2/datacenters",
            "target": {"name": target_name},
            "params": {},
        },
    )
    assert result["status"] == "ok"
    after_first_keys = list(instance._creds._cache.keys())  # type: ignore[attr-defined]
    assert cache_key in after_first_keys, (
        f"Expected credentials cached after first dispatch; got keys={after_first_keys!r}"
    )

    # Second dispatch must NOT reload credentials — cache entry survives.
    # Uses a still-ingested op (request list); environments moved to the
    # typed surface (T4 · #2304) and no longer dispatches via this path.
    creds_before = dict(instance._creds._cache)  # type: ignore[attr-defined]
    result2 = await call_operation(
        _OPERATOR,
        {
            "connector_id": FLEET_CONNECTOR_ID,
            "op_id": "GET:/lcm/request/api/v2/requests",
            "target": {"name": target_name},
            "params": {},
        },
    )
    assert result2["status"] == "ok"
    assert instance._creds._cache == creds_before, (  # type: ignore[attr-defined]
        "Credential cache should not change on second dispatch (cache hit); "
        f"before={creds_before!r} after={instance._creds._cache!r}"  # type: ignore[attr-defined]
    )


async def test_fleet_e2e_dispatch_writes_audit_row(
    fleet_e2e_canary: _FleetE2EBundle,
) -> None:
    """Each dispatch inserts an AuditLog row with method='DISPATCH', target_id, params_hash.

    Exercises acceptance criterion (d). Dispatches one op and asserts:

    * Exactly one new ``AuditLog`` row with ``method='DISPATCH'`` and
      ``path == op_id``.
    * ``row.target_id`` is not None (the Target row was resolved and
      its UUID was recorded).
    * ``row.payload["op_id"]`` equals the dispatched ``op_id``.
    * ``row.payload["params_hash"]`` is present (non-None string).
    """
    op_id = "GET:/lcm/lcops/api/v2/datacenters"
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
            "connector_id": FLEET_CONNECTOR_ID,
            "op_id": op_id,
            "target": {"name": fleet_e2e_canary.target_name},
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


async def test_fleet_e2e_jsonflux_handle_populated_for_request_list(
    fleet_e2e_canary: _FleetE2EBundle,
) -> None:
    """Request list dispatched with the real JsonFluxReducer returns a populated handle.

    Exercises acceptance criterion (c): the JSONFlux dispatcher seam
    threads the reducer's :class:`ResultHandle` onto
    :class:`OperationResult`. Lifecycle-request listing is the largest
    *ingested* Fleet read surface in real deployments — busy appliances
    accumulate thousands of historical requests — so the ingested-path
    handle coverage lands here. (Environment listing, formerly used here,
    is a typed op now — T4 · #2304.)

    Assertions mirror the NSX / SDDC Manager JSONFlux handle tests:

    * ``status == 'ok'``.
    * ``handle`` is non-None and carries a valid UUID4 ``handle_id``.
    * ``handle.total_rows`` matches the seeded request count.
    * ``handle.sample_rows`` is populated (≥1 row).
    * ``result['row_count']`` equals ``handle.total_rows`` (the
      reducer's summary inlined on the envelope).
    """
    expected_rows = len(FLEET_CANARY_REQUESTS)

    set_default_reducer(JsonFluxReducer(row_threshold=0))
    try:
        result_envelope = await call_operation(
            _OPERATOR,
            {
                "connector_id": FLEET_CONNECTOR_ID,
                "op_id": FLEET_FORCE_HANDLE_LIST_OP_ID,
                "target": {"name": fleet_e2e_canary.target_name},
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
        f"Expected {expected_rows} request rows from FLEET_CANARY_REQUESTS; "
        f"got handle.total_rows={handle['total_rows']}"
    )

    sample_rows = handle.get("sample_rows")
    assert sample_rows, (
        f"Expected ≥1 sample row from the seeded Fleet request list; "
        f"got sample_rows={sample_rows!r}"
    )

    payload = result_envelope.get("result")
    assert payload is not None and payload.get("row_count") == expected_rows, (
        f"Expected reducer summary on result.row_count={expected_rows}; got result={payload!r}"
    )
