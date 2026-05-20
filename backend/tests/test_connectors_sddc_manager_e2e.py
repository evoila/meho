# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""SDDC Manager E2E recorded-fixture integration test (G3.5-T6 #618).

Covers all four acceptance criteria from Issue #618:

(a) All 9 curated SDDC Manager read ops dispatch through the full
    ``call_operation`` stack against a respx-mocked SDDC Manager
    appliance and return ``status='ok'``.
(b) HTTP Basic auth path: no 401-retry for SDDC Manager — Basic
    credentials are stateless. The connector's stub credentials loader
    is verified to be called (once, then cached) on the first dispatch.
(c) Audit rows: each dispatch inserts an ``AuditLog`` row carrying
    ``method='DISPATCH'``, a non-null ``target_id``, and a
    ``payload["params_hash"]`` key.
(d) JSONFlux handle path: ``GET:/v1/hosts`` dispatched with a
    ``ForceHandleReducer`` returns a populated ``OperationResult.handle``
    with at least one ``sample_rows`` entry.

Database: SQLite via the autouse ``_default_database_url`` conftest
fixture (no ``pg_engine`` required). Runs in the ``meho-runners``
CI lane alongside the other ``backend/tests/test_connectors_*.py``
files; no Docker dependency.
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
from meho_backplane.connectors.registry import all_connectors_v2
from meho_backplane.connectors.schemas import ResultHandle
from meho_backplane.connectors.sddc_manager import (
    SDDC_CONNECTOR_ID,
    SDDC_CORE_OPS,
    SDDC_IMPL_ID,
    SDDC_VERSION,
    SddcManagerConnector,
)
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import AuditLog, Target
from meho_backplane.operations import reset_dispatcher_caches
from meho_backplane.operations._handler_resolve import get_or_create_connector_instance
from meho_backplane.operations.dispatcher import set_default_reducer
from meho_backplane.operations.meta_tools import call_operation
from meho_backplane.operations.reducer import PassThroughReducer
from tests.acceptance._sddc_canary_fixtures import (
    SDDC_CANARY_BASE_URL,
    SDDC_CANARY_FINGERPRINT,
    SDDC_CANARY_HOSTS,
    SDDC_CANARY_OPERATOR_TENANT,
    SDDC_FORCE_HANDLE_LIST_OP_ID,
    _insert_sddc_descriptors,
    _register_sddc_routes,
    _sddc_credentials_loader,
)

# ---------------------------------------------------------------------------
# Module-level constants
# ---------------------------------------------------------------------------

_OPERATOR = Operator(
    sub="sddc-e2e-test",
    name="SDDC E2E Test Operator",
    email=None,
    raw_jwt="<sddc-e2e-raw-jwt>",
    tenant_id=SDDC_CANARY_OPERATOR_TENANT,
    tenant_role=TenantRole.TENANT_ADMIN,
)

_E2E_TARGET_NAME = "sddc-e2e-target"

# Path-template params for the domain info op whose URL carries ``{id}``.
_DOMAIN_INFO_OP_PARAMS: dict[str, dict[str, object]] = {
    "GET:/v1/domains/{id}": {"id": "domain-mgmt"},
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
    """Stub out :func:`publish_event` so the broadcast bus doesn't fire."""
    events: list[Any] = []

    async def _capture(event: Any) -> None:
        events.append(event)

    monkeypatch.setattr(audit_module, "publish_event", _capture)
    return events


# ---------------------------------------------------------------------------
# ForceHandleReducer (acceptance criterion d)
# ---------------------------------------------------------------------------


class _ForceHandleReducer:
    """Test-only reducer that always wraps an SDDC Manager list payload in a ResultHandle."""

    async def reduce(
        self,
        payload: Any,
        schema: dict[str, Any] | None = None,
        context: dict[str, Any] | None = None,
    ) -> tuple[Any, ResultHandle | None]:
        del schema, context
        # SDDC Manager wraps lists in {"elements": [...]}
        if isinstance(payload, dict) and "elements" in payload:
            rows = payload["elements"]
            total = len(rows) if isinstance(rows, list) else 1
            sample: tuple[Any, ...] = tuple(rows[:5]) if isinstance(rows, list) and rows else ()
        elif isinstance(payload, list):
            total = len(payload)
            sample = tuple(payload[:5]) if payload else ()
        else:
            total = 1
            sample = ()
        handle = ResultHandle(
            handle_id=uuid.uuid4(),
            summary_md=f"force-mode handle ({total} rows)",
            schema_={"type": "array", "items": {"type": "object"}},
            total_rows=total,
            sample_rows=sample or None,
            ttl_seconds=3600,
        )
        return {"row_count": total, "sample": list(sample)}, handle


# ---------------------------------------------------------------------------
# Setup helpers
# ---------------------------------------------------------------------------


async def _seed_target() -> Any:
    """Insert the E2E target row and return it (expunged from the session)."""
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        target = Target(
            tenant_id=SDDC_CANARY_OPERATOR_TENANT,
            name=_E2E_TARGET_NAME,
            aliases=[],
            product="sddc-manager",
            host=SDDC_CANARY_BASE_URL.removeprefix("https://"),
            port=443,
            fqdn=None,
            secret_ref="kv/data/sddc-manager/sddc-e2e",
            auth_model="shared_service_account",
            vpn_required=False,
            extras={},
            fingerprint=SDDC_CANARY_FINGERPRINT,
            notes="seeded by test_connectors_sddc_manager_e2e._seed_target",
        )
        session.add(target)
        await session.commit()
        await session.refresh(target)
        session.expunge(target)
        return target


def _resolve_connector() -> SddcManagerConnector:
    """Resolve + cache the SddcManagerConnector instance with a stubbed credentials loader."""
    registry = all_connectors_v2()
    connector_cls = registry.get(("sddc-manager", SDDC_VERSION, SDDC_IMPL_ID))
    assert connector_cls is SddcManagerConnector, (
        f"SddcManagerConnector not registered for "
        f"(sddc-manager, {SDDC_VERSION}, {SDDC_IMPL_ID}); got {connector_cls!r}"
    )
    instance = get_or_create_connector_instance(connector_cls)
    instance._credentials_loader = _sddc_credentials_loader  # type: ignore[attr-defined]
    return instance


# ---------------------------------------------------------------------------
# Primary E2E fixture (happy-path + audit rows)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _SddcE2EBundle:
    target_name: str
    connector_instance: SddcManagerConnector


@pytest.fixture
async def sddc_e2e_canary(captured_events: list[Any]) -> AsyncIterator[_SddcE2EBundle]:
    """Dispatcher-ready SDDC Manager setup over a respx-mocked appliance (happy-path).

    Lifecycle:
    1. Insert :data:`~meho_backplane.connectors.sddc_manager.SDDC_CORE_OPS`
       descriptors + groups into the per-test SQLite DB.
    2. Seed a :class:`Target` row carrying :data:`SDDC_CANARY_FINGERPRINT`
       so the resolver binds :class:`SddcManagerConnector`.
    3. Resolve + cache the connector instance; patch its
       ``_credentials_loader`` to bypass Vault.
    4. Activate a respx router for :data:`SDDC_CANARY_BASE_URL` and
       register the 9 read-op routes.
    """
    await _insert_sddc_descriptors()
    await _seed_target()
    instance = _resolve_connector()

    async with respx.mock(
        base_url=SDDC_CANARY_BASE_URL,
        assert_all_called=False,
        assert_all_mocked=False,
    ) as mock:
        _register_sddc_routes(mock)
        try:
            yield _SddcE2EBundle(
                target_name=_E2E_TARGET_NAME,
                connector_instance=instance,
            )
        finally:
            await instance.aclose()
            reset_dispatcher_caches()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

_OP_IDS: tuple[str, ...] = tuple(op.op_id for op in SDDC_CORE_OPS)
assert len(_OP_IDS) == 9, f"Expected 9 curated SDDC Manager ops, got {len(_OP_IDS)}: {_OP_IDS}"


@pytest.mark.parametrize("op_id", _OP_IDS, ids=lambda op: op)
async def test_sddc_e2e_all_ops_dispatch_ok(
    op_id: str,
    sddc_e2e_canary: _SddcE2EBundle,
) -> None:
    """All 9 SDDC Manager core ops dispatch through the full dispatcher and return status='ok'.

    HTTP Basic auth is computed by the connector's stub credentials loader on
    first dispatch (then cached); no session-establish step is needed because
    Basic auth is stateless on the SDDC Manager side.
    """
    params = _DOMAIN_INFO_OP_PARAMS.get(op_id, {})
    result = await call_operation(
        _OPERATOR,
        {
            "connector_id": SDDC_CONNECTOR_ID,
            "op_id": op_id,
            "target": {"name": sddc_e2e_canary.target_name},
            "params": params,
        },
    )
    assert result["status"] == "ok", (
        f"SDDC Manager op {op_id!r} did not return status='ok': "
        f"error={result.get('error')!r} full={result!r}"
    )


async def test_sddc_e2e_credentials_cached_after_first_dispatch(
    sddc_e2e_canary: _SddcE2EBundle,
) -> None:
    """First dispatch against a fresh target loads credentials; subsequent dispatches reuse cache.

    Verifies acceptance criterion (b): the SDDC Manager connector's per-target
    credential cache starts empty, gets populated on the first dispatch, and is
    not re-filled on the second dispatch to the same target. HTTP Basic is
    stateless server-side — no session revoke or re-login needed.
    """
    instance = sddc_e2e_canary.connector_instance
    target_name = sddc_e2e_canary.target_name

    assert target_name not in instance._creds_cache, (
        "Expected empty credential cache before first dispatch; "
        f"got _creds_cache={list(instance._creds_cache.keys())!r}"
    )

    result = await call_operation(
        _OPERATOR,
        {
            "connector_id": SDDC_CONNECTOR_ID,
            "op_id": "GET:/v1/releases/system",
            "target": {"name": target_name},
            "params": {},
        },
    )
    assert result["status"] == "ok"
    assert target_name in instance._creds_cache, (
        "Expected credentials cached after first dispatch; "
        f"got _creds_cache={list(instance._creds_cache.keys())!r}"
    )

    # Second dispatch must NOT reload credentials — cache entry survives.
    creds_before = dict(instance._creds_cache)
    result2 = await call_operation(
        _OPERATOR,
        {
            "connector_id": SDDC_CONNECTOR_ID,
            "op_id": "GET:/v1/sddc-managers",
            "target": {"name": target_name},
            "params": {},
        },
    )
    assert result2["status"] == "ok"
    assert instance._creds_cache == creds_before, (
        "Credential cache should not change on second dispatch (cache hit); "
        f"before={creds_before!r} after={instance._creds_cache!r}"
    )


async def test_sddc_e2e_dispatch_writes_audit_row(
    sddc_e2e_canary: _SddcE2EBundle,
) -> None:
    """Each dispatch inserts an AuditLog row with method='DISPATCH', target_id, params_hash.

    Exercises acceptance criterion (c). Dispatches one op and asserts:

    * Exactly one new ``AuditLog`` row with ``method='DISPATCH'`` and
      ``path == op_id``.
    * ``row.target_id`` is not None (the Target row was resolved and
      its UUID was recorded).
    * ``row.payload["op_id"]`` equals the dispatched ``op_id``.
    * ``row.payload["params_hash"]`` is present (non-None string).
    """
    op_id = "GET:/v1/domains"
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
            "connector_id": SDDC_CONNECTOR_ID,
            "op_id": op_id,
            "target": {"name": sddc_e2e_canary.target_name},
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


async def test_sddc_e2e_jsonflux_handle_populated_for_host_list(
    sddc_e2e_canary: _SddcE2EBundle,
) -> None:
    """Host list dispatched with ForceHandleReducer returns a populated handle.

    Exercises acceptance criterion (d): the JSONFlux dispatcher seam
    threads the reducer's :class:`ResultHandle` onto
    :class:`OperationResult`. Host list is the largest SDDC Manager read
    surface in a typical VCF deployment — the same posture NSX uses
    ``GET:/policy/api/v1/infra/segments`` for.

    Assertions mirror the NSX JSONFlux handle test:

    * ``status == 'ok'``.
    * ``handle`` is non-None and carries a valid UUID4 ``handle_id``.
    * ``handle.total_rows`` matches the seeded host count.
    * ``handle.sample_rows`` is populated (≥1 row).
    * ``result['row_count']`` equals ``handle.total_rows``.
    """
    expected_rows = len(SDDC_CANARY_HOSTS["elements"])  # type: ignore[arg-type]

    set_default_reducer(_ForceHandleReducer())
    try:
        result_envelope = await call_operation(
            _OPERATOR,
            {
                "connector_id": SDDC_CONNECTOR_ID,
                "op_id": SDDC_FORCE_HANDLE_LIST_OP_ID,
                "target": {"name": sddc_e2e_canary.target_name},
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
        "Expected OperationResult.handle to be populated by _ForceHandleReducer; "
        f"got handle=None on envelope={result_envelope!r}"
    )

    uuid.UUID(handle["handle_id"])

    assert handle["total_rows"] == expected_rows, (
        f"Expected {expected_rows} host rows from SDDC_CANARY_HOSTS; "
        f"got handle.total_rows={handle['total_rows']}"
    )

    sample_rows = handle.get("sample_rows")
    assert sample_rows, (
        f"Expected ≥1 sample row from the seeded SDDC host list; got sample_rows={sample_rows!r}"
    )

    payload = result_envelope.get("result")
    assert payload is not None and payload.get("row_count") == expected_rows, (
        f"Expected reducer summary on result.row_count={expected_rows}; got result={payload!r}"
    )
