# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""NSX E2E recorded-fixture integration test (G3.5-T3 #615).

Covers all four acceptance criteria from Issue #615:

(a) All 9 curated NSX read ops dispatch through the full
    ``call_operation`` stack against a respx-mocked NSX manager
    and return ``status='ok'``.
(b) Session-establish + 401-retry path: the connector fires
    ``POST /api/session/create`` on the first dispatch, and a
    simulated 401 from the NSX manager side triggers one re-login +
    one retry (no more, no less).
(c) Audit rows: each dispatch inserts an ``AuditLog`` row carrying
    ``method='DISPATCH'``, a non-null ``target_id``, and a
    ``payload["params_hash"]`` key.
(d) JSONFlux handle path: ``GET:/policy/api/v1/infra/segments``
    dispatched with a ``ForceHandleReducer`` returns a populated
    ``OperationResult.handle`` with at least one ``sample_rows``
    entry.

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

import httpx
import pytest
import respx
from sqlalchemy import select

import meho_backplane.operations._audit as audit_module
from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors.nsx import (
    NSX_CONNECTOR_ID,
    NSX_CORE_OPS,
    NSX_IMPL_ID,
    NSX_PRODUCT,
    NSX_VERSION,
    NsxConnector,
)
from meho_backplane.connectors.registry import all_connectors_v2
from meho_backplane.connectors.schemas import ResultHandle
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import AuditLog, Target
from meho_backplane.operations import reset_dispatcher_caches
from meho_backplane.operations._handler_resolve import get_or_create_connector_instance
from meho_backplane.operations.dispatcher import set_default_reducer
from meho_backplane.operations.meta_tools import call_operation
from meho_backplane.operations.reducer import PassThroughReducer
from tests.acceptance._nsx_canary_fixtures import (
    NSX_CANARY_BASE_URL,
    NSX_CANARY_FINGERPRINT,
    NSX_CANARY_OPERATOR_TENANT,
    NSX_CANARY_SEGMENTS,
    NSX_FORCE_HANDLE_LIST_OP_ID,
    _insert_nsx_descriptors,
    _nsx_session_loader,
    _register_nsx_routes,
)

# ---------------------------------------------------------------------------
# Module-level constants
# ---------------------------------------------------------------------------

_OPERATOR = Operator(
    sub="nsx-e2e-test",
    name="NSX E2E Test Operator",
    email=None,
    raw_jwt="<nsx-e2e-raw-jwt>",
    tenant_id=NSX_CANARY_OPERATOR_TENANT,
    tenant_role=TenantRole.TENANT_ADMIN,
)

_E2E_TARGET_NAME = "nsx-e2e-target"

# Path-template params for the two firewall ops whose URLs carry
# ``{domain-id}`` / ``{security-policy-id}`` placeholders. The
# dispatcher's ``_substitute_path`` fills them at dispatch time;
# the respx routes are registered against the substituted URLs.
_FIREWALL_OP_PARAMS: dict[str, dict[str, object]] = {
    "GET:/policy/api/v1/infra/domains/{domain-id}/security-policies": {
        "domain-id": "default",
    },
    "GET:/policy/api/v1/infra/domains/{domain-id}/security-policies/{security-policy-id}/rules": {
        "domain-id": "default",
        "security-policy-id": "policy-app-tier",
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
# ForceHandleReducer (acceptance criterion d)
# ---------------------------------------------------------------------------


class _ForceHandleReducer:
    """Test-only reducer that always wraps an NSX list payload in a ResultHandle.

    Installed for the JSONFlux handle-path test only; all other tests
    use the v0.2 default :class:`PassThroughReducer`.
    """

    async def reduce(
        self,
        payload: Any,
        schema: dict[str, Any] | None = None,
        context: dict[str, Any] | None = None,
    ) -> tuple[Any, ResultHandle | None]:
        del schema, context
        if isinstance(payload, dict) and "results" in payload:
            rows = payload["results"]
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
# Setup helper
# ---------------------------------------------------------------------------


async def _seed_target() -> Any:
    """Insert the E2E target row and return it (expunged from the session)."""
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        target = Target(
            tenant_id=NSX_CANARY_OPERATOR_TENANT,
            name=_E2E_TARGET_NAME,
            aliases=[],
            product=NSX_PRODUCT,
            host=NSX_CANARY_BASE_URL.removeprefix("https://"),
            port=443,
            fqdn=None,
            secret_ref="kv/data/nsx/nsx-e2e",
            auth_model="shared_service_account",
            vpn_required=False,
            extras={},
            fingerprint=NSX_CANARY_FINGERPRINT,
            notes="seeded by test_connectors_nsx_e2e._seed_target",
        )
        session.add(target)
        await session.commit()
        await session.refresh(target)
        session.expunge(target)
        return target


def _resolve_connector() -> NsxConnector:
    """Resolve + cache the NsxConnector instance with a stubbed session loader."""
    registry = all_connectors_v2()
    connector_cls = registry.get((NSX_PRODUCT, NSX_VERSION, NSX_IMPL_ID))
    assert connector_cls is NsxConnector, (
        f"NsxConnector not registered for ({NSX_PRODUCT}, {NSX_VERSION}, {NSX_IMPL_ID}); "
        f"got {connector_cls!r}"
    )
    instance = get_or_create_connector_instance(connector_cls)
    instance._session_loader = _nsx_session_loader  # type: ignore[attr-defined]
    return instance


# ---------------------------------------------------------------------------
# Primary E2E fixture (happy-path + audit rows)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _NsxE2EBundle:
    target_name: str
    connector_instance: NsxConnector


@pytest.fixture
async def nsx_e2e_canary(captured_events: list[Any]) -> AsyncIterator[_NsxE2EBundle]:
    """Dispatcher-ready NSX setup over a respx-mocked manager (standard happy-path).

    Lifecycle:
    1. Insert :data:`~meho_backplane.connectors.nsx.NSX_CORE_OPS`
       descriptors + groups into the per-test SQLite DB.
    2. Seed a :class:`Target` row carrying :data:`NSX_CANARY_FINGERPRINT`
       so the resolver binds :class:`NsxConnector`.
    3. Resolve + cache the connector instance; patch its
       ``_session_loader`` to bypass Vault.
    4. Activate a respx router for :data:`NSX_CANARY_BASE_URL` and
       register the 9 read-op routes + session-create.
    """
    await _insert_nsx_descriptors()
    await _seed_target()
    instance = _resolve_connector()

    async with respx.mock(
        base_url=NSX_CANARY_BASE_URL,
        assert_all_called=False,
        assert_all_mocked=False,
    ) as mock:
        _register_nsx_routes(mock)
        try:
            yield _NsxE2EBundle(
                target_name=_E2E_TARGET_NAME,
                connector_instance=instance,
            )
        finally:
            await instance.aclose()
            reset_dispatcher_caches()


# ---------------------------------------------------------------------------
# 401-retry fixture (acceptance criterion b)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Nsx401RetryBundle:
    connector_instance: NsxConnector
    db_target: Any
    session_route: Any
    node_route: Any


@pytest.fixture
async def nsx_e2e_401_canary(
    captured_events: list[Any],
) -> AsyncIterator[_Nsx401RetryBundle]:
    """NSX setup with a respx mock that simulates a 401 on the first node GET.

    The session-create route answers twice (initial establish + post-401
    re-login). ``GET /api/v1/node`` returns 401 first then 200, exercising
    :meth:`NsxConnector._get_json_with_session_retry`'s single-retry
    contract.

    The ``dispatch_ingested`` dispatcher branch calls ``_request_json``
    directly (no 401-retry at the dispatch level — 4xx propagate as
    ``connector_error`` per the ingested-dispatch contract). The 401-retry
    path is tested here through the connector method directly against a
    DB-seeded :class:`Target`, which is the real production call site
    (``fingerprint`` / ``probe`` and any future typed ops that call
    ``_get_json_with_session_retry``).
    """
    await _insert_nsx_descriptors()
    target = await _seed_target()
    instance = _resolve_connector()

    async with respx.mock(
        base_url=NSX_CANARY_BASE_URL,
        assert_all_called=False,
        assert_all_mocked=False,
    ) as mock:
        session_route = mock.post("/api/session/create")
        session_route.side_effect = [
            httpx.Response(
                200,
                headers={
                    "X-XSRF-TOKEN": "xsrf-first",
                    "Set-Cookie": "JSESSIONID=js-first; Path=/; HttpOnly",
                },
            ),
            httpx.Response(
                200,
                headers={
                    "X-XSRF-TOKEN": "xsrf-second",
                    "Set-Cookie": "JSESSIONID=js-second; Path=/; HttpOnly",
                },
            ),
        ]
        node_route = mock.get("/api/v1/node")
        node_route.side_effect = [
            httpx.Response(401),
            httpx.Response(
                200,
                json={
                    "node_version": NSX_VERSION,
                    "kernel_version": "4.2.1.0.0",
                    "node_uuid": "deadbeef-retry-test",
                    "hostname": "nsx-retry.test.invalid",
                },
            ),
        ]
        try:
            yield _Nsx401RetryBundle(
                connector_instance=instance,
                db_target=target,
                session_route=session_route,
                node_route=node_route,
            )
        finally:
            await instance.aclose()
            reset_dispatcher_caches()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

_OP_IDS: tuple[str, ...] = tuple(op.op_id for op in NSX_CORE_OPS)
assert len(_OP_IDS) == 9, f"Expected 9 curated NSX ops, got {len(_OP_IDS)}: {_OP_IDS}"


@pytest.mark.parametrize("op_id", _OP_IDS, ids=lambda op: op)
async def test_nsx_e2e_all_ops_dispatch_ok(
    op_id: str,
    nsx_e2e_canary: _NsxE2EBundle,
) -> None:
    """All 9 NSX core ops dispatch through the full dispatcher and return status='ok'.

    Each op exercises session-establish (first call in the series) or
    re-uses the cached XSRF token (subsequent calls). The parametrise
    reports one CI case per op_id for granular failure attribution.
    """
    params = _FIREWALL_OP_PARAMS.get(op_id, {})
    result = await call_operation(
        _OPERATOR,
        {
            "connector_id": NSX_CONNECTOR_ID,
            "op_id": op_id,
            "target": {"name": nsx_e2e_canary.target_name},
            "params": params,
        },
    )
    assert result["status"] == "ok", (
        f"NSX op {op_id!r} did not return status='ok': "
        f"error={result.get('error')!r} full={result!r}"
    )


async def test_nsx_e2e_session_establishes_on_first_dispatch(
    nsx_e2e_canary: _NsxE2EBundle,
) -> None:
    """First dispatch to a fresh target fires POST /api/session/create.

    Verifies the session-establish half of acceptance criterion (b) by
    inspecting the connector's XSRF token cache before and after the
    first ``call_operation`` call. The token key is ``target.name``,
    the value is the ``X-XSRF-TOKEN`` the mock returns.
    """
    instance = nsx_e2e_canary.connector_instance
    target_name = nsx_e2e_canary.target_name

    assert target_name not in instance._session_tokens, (
        "Expected empty token cache before first dispatch; "
        f"got _session_tokens={instance._session_tokens!r}"
    )

    result = await call_operation(
        _OPERATOR,
        {
            "connector_id": NSX_CONNECTOR_ID,
            "op_id": "GET:/api/v1/node",
            "target": {"name": target_name},
            "params": {},
        },
    )
    assert result["status"] == "ok"
    assert instance._session_tokens.get(target_name) == "canary-xsrf-token", (
        f"Expected XSRF token cached after first dispatch; "
        f"got _session_tokens={instance._session_tokens!r}"
    )


async def test_nsx_e2e_401_retry_via_connector_method(
    nsx_e2e_401_canary: _Nsx401RetryBundle,
) -> None:
    """A 401 on the first node GET triggers one re-login + one retry.

    Exercises acceptance criterion (b) via
    :meth:`NsxConnector._get_json_with_session_retry` against a
    DB-seeded :class:`Target`.

    The ``dispatch_ingested`` path calls ``_request_json`` directly (no
    401-retry at that level; a 401 becomes ``connector_error``). The
    retry contract lives on ``_get_json_with_session_retry``, which is
    the call site for ``fingerprint()`` / ``probe()`` and any future
    typed ops. Testing through the connector method (not ``call_operation``)
    is the correct E2E surface for this path.

    Assertions:
    * The call returns the node JSON successfully (retry succeeded).
    * ``POST /api/session/create`` called exactly twice: initial +
      post-401 re-login.
    * ``GET /api/v1/node`` called exactly twice: 401 + successful retry.
    * Post-retry XSRF token (``xsrf-second``) replaces the stale one.
    """
    bundle = nsx_e2e_401_canary

    result = await bundle.connector_instance._get_json_with_session_retry(
        bundle.db_target,
        "/api/v1/node",
        operator=_OPERATOR,
    )

    assert result.get("node_version") == NSX_VERSION, (
        f"Expected node_version={NSX_VERSION!r} in retry result; got {result!r}"
    )
    assert bundle.session_route.call_count == 2, (
        f"Expected session-create called twice (initial + post-401 relogin); "
        f"got call_count={bundle.session_route.call_count}"
    )
    assert bundle.node_route.call_count == 2, (
        f"Expected GET /api/v1/node called twice (401 + retry); "
        f"got call_count={bundle.node_route.call_count}"
    )
    assert bundle.connector_instance._session_tokens.get(_E2E_TARGET_NAME) == "xsrf-second", (
        f"Expected post-retry XSRF token to be 'xsrf-second'; "
        f"got _session_tokens={bundle.connector_instance._session_tokens!r}"
    )


async def test_nsx_e2e_dispatch_writes_audit_row(
    nsx_e2e_canary: _NsxE2EBundle,
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
    op_id = "GET:/api/v1/node"
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
            "connector_id": NSX_CONNECTOR_ID,
            "op_id": op_id,
            "target": {"name": nsx_e2e_canary.target_name},
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


async def test_nsx_e2e_jsonflux_handle_populated_for_segment_list(
    nsx_e2e_canary: _NsxE2EBundle,
) -> None:
    """Segment list dispatched with ForceHandleReducer returns a populated handle.

    Exercises acceptance criterion (d): the JSONFlux dispatcher seam
    threads the reducer's :class:`ResultHandle` onto
    :class:`OperationResult`.

    Assertions mirror :mod:`tests.acceptance.test_g35_nsx_jsonflux_force_handle`:

    * ``status == 'ok'``.
    * ``handle`` is non-None and carries a valid UUID4 ``handle_id``.
    * ``handle.total_rows`` matches the seeded segment count.
    * ``handle.sample_rows`` is populated (≥1 row).
    * ``result['row_count']`` equals ``handle.total_rows`` (the
      reducer's summary inlined on the envelope).
    """
    expected_rows = len(NSX_CANARY_SEGMENTS["results"])  # type: ignore[arg-type]

    set_default_reducer(_ForceHandleReducer())
    try:
        result_envelope = await call_operation(
            _OPERATOR,
            {
                "connector_id": NSX_CONNECTOR_ID,
                "op_id": NSX_FORCE_HANDLE_LIST_OP_ID,
                "target": {"name": nsx_e2e_canary.target_name},
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
        f"Expected {expected_rows} segment rows from NSX_CANARY_SEGMENTS; "
        f"got handle.total_rows={handle['total_rows']}"
    )

    sample_rows = handle.get("sample_rows")
    assert sample_rows, (
        f"Expected ≥1 sample row from the seeded NSX segment list; got sample_rows={sample_rows!r}"
    )

    payload = result_envelope.get("result")
    assert payload is not None and payload.get("row_count") == expected_rows, (
        f"Expected reducer summary on result.row_count={expected_rows}; got result={payload!r}"
    )
